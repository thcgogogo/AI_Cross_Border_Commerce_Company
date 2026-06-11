"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let AGENTS = [];           // org chart from the server
let AGENT_IDS = new Set(); // quick "is this a real agent?" lookup
let MODEL_CHOICES = [];    // models a user can assign to an agent (for the dropdowns)
let INDUSTRIES = [];       // industry template packs (task-form override dropdown)
let selectedFiles = [];    // files the user picked (our own list so we can remove)
let pollTimer = null;
let lastReport = "";       // raw markdown of the final report (for Copy)
let drawerOpen = false;    // is the side menu open?
let tokenTimer = null;     // polls /api/tokens while the drawer is open

// Detail window (Feature 2) — a tiny history stack so Back/Forward work.
let lastSnapshot = null;   // most recent /api/status payload (for live detail re-render)
let lastTokens = null;     // most recent /api/tokens payload (for live detail re-render)
let detailHistory = [];    // stack of view names: "chatbot" | "fusion" | "team" | "models" | "unified"
let detailIndex = -1;      // pointer into detailHistory
let detailOpen = false;

// Providers (for grouping models by company on the Models + Unified API pages).
let PROVIDERS = {};        // provider id -> display label

// Money + model-function metadata (from /api/meta). Everything money-related is
// shown in Chinese Yuan (¥); the rate + symbol + the model "kinds" live here.
let META = null;           // { currency, currency_symbol, usd_to_cny, kinds, kind_order, ... }
let modelsKind = "text";   // the active function tab on the Models & Pricing page

// Unified-API dashboard ("Get Unified API" page) state. Model selection now lives
// ONLY behind a logged-in account (the dashboard's editable picker) — there is no
// anonymous storefront anymore (model selection is gated behind sign-up/login).
let dashSelected = new Set();  // model ids ticked in the dashboard's model editor
let dashSeededFor = null;      // which account id dashSelected was last seeded from

// Accounts (email + password). Login lives in the top-right widget; the
// dashboard is whichever account is signed into THIS browser session.
let currentAccount = null;     // public view of the logged-in account (or null)
let accountError = "";         // a dashboard refresh error to show
let signupKey = null;          // the gateway key shown ONCE right after sign-up
let signupCustomer = null;     // the account created at sign-up (for the reveal)
let signupError = "";          // a sign-up validation/server error to show

// Fusion tab — run one prompt across several models, then fuse to the best answer.
let fusionSelected = [];       // ordered model ids in the fusion "council"
let fusionFuser = "auto";      // which model reconciles them ("auto" or a model id)
let fusionPreset = "custom";   // the active preset chip (quality | budget | custom)
let fusionPrompt = "";         // the prompt text (kept across re-renders)
let fusionRunning = false;     // is a fusion run in flight?
let fusionResult = null;       // the latest {prompt, answers, fused} response
let fusionError = "";          // a fusion validation/server error to show

// Chat bot tab (the task form, now a menu workspace instead of the home page).
let goalDraft = "";            // remembers a typed-but-unsent goal across re-renders
let industryDraft = "auto";    // remembered industry-pack pick ("auto" = use company profile)
let jobRunning = false;        // is a team job in flight? (keeps the run button state)
let jobError = "";             // a transport error from starting/polling a job

// Company Brain (企业大脑) page — the team's persistent long-term memory: the
// company profile + KPI column-mappings + glossary + report preferences, plus a
// historical-import flow. Cached per account so re-renders don't refetch;
// brainSeededFor tracks which account the cache currently belongs to.
let brainSeededFor = null;     // account id the brain cache was loaded for (null = stale)
let brainLoading = false;      // a load is in flight (so the shell doesn't re-trigger it)
let brainProfile = null;       // {company_name, industry, business_desc, fiscal_week_start}
let brainKpiDraft = [];        // editable KPI-mapping rows [{kpi_name_cn, aliases, unit}]
let brainGlossary = [];        // [{id, term, definition}]
let brainPrefs = [];           // [{id, instruction, active}]
let brainError = "";           // a load/save error to show on the page
let brainMsg = "";             // a transient success line (e.g. "已保存")
let brainImport = null;        // historical-import flow state (see openBrainImport)
let brainImportTimer = null;   // polls /api/company-brain/import/<job_id> while running
let brainBranding = null;      // {brand_color, wechat_webhook, has_logo} (Phase 5)
let brainJobs = [];            // [{id, name_cn, cron_expr, delivery_channels, active, …}]
let brainJobDraft = null;      // the "new 定时任务" form state (preserved across re-renders)
let brainReports = [];         // [{id, status, goal_text, has_pdf, download_url, …}]
let confirmedTermsJob = null;  // job_id whose UNKNOWN_TERMS banner was confirmed/dismissed

// Onboarding wizard (Phase 10) — 5 steps, skippable, resumable.
let obOpen = false;            // is the wizard overlay showing?
let obState = null;            // last /api/onboarding/* state payload
let obStep = 1;                // the step currently shown (1..5)
let obBusy = false;            // a step action is in flight
let obError = "";              // step-level error line
let obData = null;             // step-2 result {file_name, headers, kpi_guesses}
let obJobId = null;            // step-4 run id (polls /api/status)
let obSnap = null;             // step-4 live snapshot (feeds the wizard's feed box)
let obPollTimer = null;
let obScheduleDone = null;     // step-5 result {job_id, channels}
let changelogHasNew = false;   // drives the 新功能 badge in the drawer + menu dot

// Voice input (Feature 1)
let recognition = null;    // SpeechRecognition instance (or null if unsupported)
let recognizing = false;

// ---------------------------------------------------------------------------
// Elements
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
// The task form AND its live progress feed + final report now live inside the
// "Chat bot" detail tab (the old Live Activity tab + home report card are gone),
// so none of those elements exist at page load. We look everything up lazily via
// $() whenever that tab is mounted. orgChartEl stays null-safe (no static node).
const orgChartEl = $("orgChart");

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
bootstrap();   // load the team + model choices + provider labels + session
wireDrawer();
wireMenuRects(); // the launcher rectangles → open the workspace pages
wireHome();      // the hero's two buttons (Get API Key / Explore Models)
wireDetail();    // full-screen workspace pages (incl. the Chat bot task form)
wireAccount();   // top-right login / sign-up / account widget
// The Chat bot task form (file inputs, mic, run button) is wired inside
// wireDetailBody() each time that tab mounts — see the bottom of that function.

// Load the team + the model choices + provider labels up front so the Team,
// Models and Unified-API pages can draw the moment they're opened.
async function bootstrap() {
  await Promise.all([loadAgents(), loadModels(), loadProviders(), loadMeta(),
                     loadIndustries(), loadMe(), loadChangelogState()]);
  wireSupport();     // the floating 联系支持 widget (needs META for QR + mailto)
  wireOnboarding();  // wizard chrome (close button); content renders on open
  routeFromPath(); // deep links: /company-brain opens the brain page, /login the modal
  maybeAutoOpenOnboarding(); // first login → the wizard, until done or dismissed
}

// Honour the URL the page was opened at. The server already 302s logged-out
// visitors away from /company-brain to /login, so by the time we get here the
// path reflects what the user is actually allowed to see.
function routeFromPath() {
  const path = location.pathname;
  if (path === "/company-brain" && currentAccount) {
    openDetail("brain");
  } else if (path === "/login" && !currentAccount) {
    openLoginModal();
  }
  // The day-3 nudge email links here: ?onboarding=demo opens the wizard straight
  // onto the demo-data step (after login, if needed).
  const wantsDemo = new URLSearchParams(location.search).get("onboarding") === "demo";
  if (wantsDemo) {
    if (currentAccount) openOnboarding({ demo: true });
    else openLoginModal();
  }
}

// ---------------------------------------------------------------------------
// Org chart
// ---------------------------------------------------------------------------
async function loadAgents() {
  try {
    const res = await fetch("/api/agents");
    AGENTS = await res.json();
    AGENT_IDS = new Set(AGENTS.map((a) => a.id));
    renderOrgChart(null);
  } catch (e) {
    orgChartEl.innerHTML = `<p class="muted">Couldn't load the team.</p>`;
  }
}

async function loadModels() {
  try {
    const res = await fetch("/api/models");
    if (res.ok) MODEL_CHOICES = await res.json();
  } catch (e) { /* leave empty — the picker will simply show nothing to choose */ }
}

// Industry template packs for the task-form override dropdown. Loaded up front so
// the Chat bot tab can offer "自动 / 按公司档案" + one option per pack on disk.
async function loadIndustries() {
  try {
    const res = await fetch("/api/industries");
    if (res.ok) INDUSTRIES = await res.json();
  } catch (e) { /* leave empty — the dropdown still offers the "auto" option */ }
}

async function loadProviders() {
  try {
    const res = await fetch("/api/providers");
    if (res.ok) {
      const list = await res.json();
      PROVIDERS = {};
      list.forEach((p) => { PROVIDERS[p.id] = p.label; });
    }
  } catch (e) { /* fall back to raw provider ids if labels can't load */ }
}

// Money + model-function metadata. Drives the ¥ formatter and the Models page's
// function tabs / modality lines — so the currency and model kinds aren't hard-coded.
async function loadMeta() {
  try {
    const res = await fetch("/api/meta");
    if (res.ok) META = await res.json();
  } catch (e) { /* fall back to ¥ defaults in cny() / a single "text" tab */ }
}

// Display label for a provider id (e.g. "anthropic" -> "Anthropic").
function providerLabel(id) {
  return PROVIDERS[id] || id || "Other";
}

// Group MODEL_CHOICES by provider, preserving the catalog's order. Returns
// [{ provider, label, items: [...] }] — used by the Models + Unified API pages.
function modelsByProvider(list) {
  const order = [];
  const groups = {};
  for (const m of (list || MODEL_CHOICES)) {
    const p = m.provider || "anthropic";
    if (!groups[p]) { groups[p] = []; order.push(p); }
    groups[p].push(m);
  }
  return order.map((p) => ({ provider: p, label: providerLabel(p), items: groups[p] }));
}

function renderOrgChart(snap) {
  if (!orgChartEl) return; // the org chart now lives only on the Team detail page
  const ceo = AGENTS.find((a) => a.reports_to === null);
  const workers = AGENTS.filter((a) => a.reports_to !== null);
  const node = (a) =>
    `<div class="org-node ${a.id === (ceo && ceo.id) ? "ceo" : ""} ${stateClass(a.id, snap)}" data-id="${a.id}">
       <div class="node-top"><span class="node-dot"></span><span class="node-name">${esc(a.name)}</span></div>
       <div class="node-title">${esc(a.title)}</div>
       <div class="node-state">${stateLabel(a.id, snap)}</div>
     </div>`;
  orgChartEl.innerHTML =
    `<div class="org-ceo">${ceo ? node(ceo) : ""}</div>` +
    `<div class="org-workers">${workers.map(node).join("")}</div>`;
}

function activeAgentId(snap) {
  if (!snap || snap.status === "done" || snap.status === "error") return null;
  const acts = snap.activity || [];
  for (let i = acts.length - 1; i >= 0; i--) {
    if (AGENT_IDS.has(acts[i].agent)) return acts[i].agent;
  }
  return null;
}

function stateClass(id, snap) {
  if (!snap) return "";
  const done = snap.status === "done";
  const hasArtifact = snap.artifacts && Object.prototype.hasOwnProperty.call(snap.artifacts, id);
  if (id === "ceo" ? done : hasArtifact) return "done";
  if (id === activeAgentId(snap)) return "active";
  return "";
}

function stateLabel(id, snap) {
  const cls = stateClass(id, snap);
  if (cls === "done") return "Done";
  if (cls === "active") return "Working…";
  return "Idle";
}

// ---------------------------------------------------------------------------
// The menu is just two launcher rectangles. Clicking one closes the menu and
// opens its full-screen page (Your Team / Live Activity) which we designed.
// ---------------------------------------------------------------------------
function wireMenuRects() {
  document.querySelectorAll(".menu-rect").forEach((btn) =>
    btn.addEventListener("click", () => {
      if (btn.dataset.action === "onboarding") {  // the 新手引导 entry
        closeDrawer();
        openOnboarding();
        return;
      }
      if (!btn.dataset.view) return;              // plain links navigate themselves
      closeDrawer();
      openDetail(btn.dataset.view);
    })
  );
}

// Change one teammate's model. Called from the dropdown inside its card on the
// Team page. The server returns the updated org chart (effective models).
async function setAgentModel(agentId, model, selEl) {
  const prev = AGENTS.find((a) => a.id === agentId);
  const prevModel = prev ? prev.model : null;
  if (selEl) selEl.disabled = true;
  try {
    const res = await fetch("/api/agents/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId, model }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not change the model.");
    AGENTS = data;
    AGENT_IDS = new Set(AGENTS.map((a) => a.id));
    refreshDetailIf(["team"]); // reflect the new pick on the Team page
  } catch (e) {
    if (selEl && prevModel) selEl.value = prevModel; // revert the dropdown on failure
    alert(e.message);
  } finally {
    if (selEl) selEl.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// File picking (drag-drop + browse)
// ---------------------------------------------------------------------------
function wireFileInputs() {
  const dz = $("dropZone");
  const fi = $("fileInput");
  if (!dz || !fi) return; // the Chat bot tab isn't mounted right now
  const browse = $("browseBtn");
  if (browse) browse.addEventListener("click", () => fi.click());
  dz.addEventListener("click", (e) => {
    if (e.target.id !== "browseBtn") fi.click();
  });
  fi.addEventListener("change", () => addFiles(fi.files));

  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => {
      e.preventDefault();
      dz.classList.remove("dragover");
    })
  );
  dz.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
}

function addFiles(fileList) {
  for (const f of fileList) {
    if (!selectedFiles.some((x) => x.name === f.name && x.size === f.size)) {
      selectedFiles.push(f);
    }
  }
  const fi = $("fileInput");
  if (fi) fi.value = ""; // allow re-picking the same file later
  renderFileList();
}

function renderFileList() {
  const fileListEl = $("fileList");
  if (!fileListEl) return; // Chat bot tab not mounted
  fileListEl.innerHTML = selectedFiles
    .map(
      (f, i) =>
        `<li><span>${esc(f.name)} · ${fmtBytes(f.size)}</span>
           <button class="rm" data-i="${i}" title="Remove">&times;</button></li>`
    )
    .join("");
  fileListEl.querySelectorAll(".rm").forEach((btn) =>
    btn.addEventListener("click", () => {
      selectedFiles.splice(Number(btn.dataset.i), 1);
      renderFileList();
    })
  );
}

// ---------------------------------------------------------------------------
// Run a job
// ---------------------------------------------------------------------------
async function startJob() {
  // The task form lives in the Chat bot tab, which is mounted right now. Its live
  // progress feed + final report render in the SAME tab (no Live Activity page).
  const goalInput = $("goal");
  const goal = (goalInput ? goalInput.value : goalDraft).trim();
  const formError = $("formError");
  if (formError) formError.hidden = true;
  if (!goal) {
    if (formError) {
      formError.textContent = "Please describe what you want the team to do.";
      formError.hidden = false;
    }
    return;
  }
  goalDraft = goal; // remember it in case the tab re-renders

  // Industry-pack pick travels with the run ("auto" = use the company profile).
  const indSel = $("industrySelect");
  const industry = indSel ? indSel.value : industryDraft;
  industryDraft = industry;

  jobError = "";
  lastSnapshot = null;   // clear any previous run's feed + report
  lastReport = "";
  setRunning(true);
  forceRefreshDetail();  // re-render the Chat bot tab to show the "starting…" state

  const fd = new FormData();
  fd.append("goal", goal);
  fd.append("industry", industry);
  selectedFiles.forEach((f) => fd.append("files", f));

  try {
    const res = await fetch("/api/run", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) {
      // Agent-team runs are billed to the logged-in account: 401 = log in first,
      // 402 = balance spent (top up on the dashboard). Surface it inline + guide.
      setRunning(false);
      jobError = data.error || "Something went wrong.";
      forceRefreshDetail();
      if (res.status === 401) openLoginModal();
      return;
    }
    pollTimer = setInterval(() => poll(data.job_id), 900);
    poll(data.job_id);
  } catch (e) {
    setRunning(false);
    jobError = e.message;
    forceRefreshDetail();  // surface the error inline in the Chat bot tab
  }
}

async function poll(jobId) {
  try {
    const res = await fetch(`/api/status/${jobId}`);
    const snap = await res.json();
    if (!res.ok) throw new Error(snap.error || "Lost contact with the server.");
    renderSnapshot(snap);
    if (snap.status === "done" || snap.status === "error") {
      clearInterval(pollTimer);
      pollTimer = null;
      setRunning(false);
    }
  } catch (e) {
    clearInterval(pollTimer);
    pollTimer = null;
    setRunning(false);
    jobError = e.message;
    refreshDetailIf(["chatbot"]); // show the transport error in the Chat bot tab
  }
}

// ---------------------------------------------------------------------------
// Render incoming state
// ---------------------------------------------------------------------------
function renderSnapshot(snap) {
  lastSnapshot = snap;
  renderOrgChart(snap);
  if (snap.status === "done" && snap.final_report) lastReport = snap.final_report;
  // The Chat bot tab renders the live feed + final report straight from lastSnapshot,
  // so just keep it (and the Team page) refreshed as new data arrives.
  refreshDetailIf(["team", "chatbot"]);
  if (detailOpen && currentView() === "team") fetchTokens(); // keep the CFO meters climbing
}

// One activity row (HTML) for the Chat bot tab's live feed. `a` is a snapshot event.
function feedRowHTML(a) {
  return `<li class="feed-item ${esc(a.type || "info")}">
      <span class="feed-badge"></span>
      <div>
        <span class="feed-who">${esc(whoName(a.agent))}</span>
        <div class="feed-msg">${esc(a.message)}</div>
      </div>
      <span class="feed-time">${fmtTime(a.ts)}</span>
    </li>`;
}

// Real, downloadable files the team produced (decks, PDFs, SVGs), as HTML. Each is
// served as an attachment from /api/files/<job>/<name>.
function deliverablesHTML(list) {
  if (!list || !list.length) return "";
  const item = (d) => {
    const ext = (String(d.filename || "").split(".").pop() || "file").toUpperCase();
    return `<a class="rf-item" href="${esc(d.url)}" download>
        <span class="rf-ext">${esc(ext)}</span>
        <span class="rf-text">
          <span class="rf-title">${esc(d.title || d.filename)}</span>
          <span class="rf-sub">${esc(d.kind)} · by ${esc(whoName(d.agent))}</span>
        </span>
        <span class="rf-dl">Download</span>
      </a>`;
  };
  return `<div class="report-files">
      <div class="rf-head">Files the team made for you (${list.length})</div>
      <div class="rf-list">${list.map(item).join("")}</div>
    </div>`;
}

// A small status pill (HTML) for the Chat bot tab header, from a job status.
function statusPillHTML(status) {
  const map = {
    starting: ["Starting…", "pill-running"],
    running: ["Working…", "pill-running"],
    done: ["Completed", "pill-done"],
    error: ["Error", "pill-error"],
  };
  const [label, cls] = map[status] || ["Idle", "pill-idle"];
  return `<span class="pill ${cls}">${label}</span>`;
}

function whoName(id) {
  if (id === "system") return "System";
  if (id === "token_manager") return "Token Manager · CFO";
  const a = AGENTS.find((x) => x.id === id);
  return a ? `${a.name} · ${a.title}` : id;
}

function setRunning(on) {
  jobRunning = on; // remembered so the button re-renders correctly if the tab remounts
  const b = $("runBtn");
  if (!b) return; // the Chat bot tab isn't mounted right now
  b.disabled = on;
  b.textContent = on ? "Team is working…" : "Start the team";
}

// ---------------------------------------------------------------------------
// Side drawer + Token Manager module
// ---------------------------------------------------------------------------
function wireDrawer() {
  $("menuBtn").addEventListener("click", openDrawer);
  $("drawerClose").addEventListener("click", closeDrawer);
  $("drawerBackdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const loginM = $("loginModal");
    if (loginM && !loginM.hidden) { closeLoginModal(); return; } // top-most overlay first…
    const signupV = $("signupView");
    if (signupV && signupV.classList.contains("open")) { closeSignup(); return; }
    if (detailOpen) { closeDetail(); return; }  // …then a full-screen page…
    if (drawerOpen) closeDrawer();              // …then the menu
  });
}

function openDrawer() {
  drawerOpen = true;
  const d = $("sideDrawer"), b = $("drawerBackdrop");
  b.hidden = false;
  requestAnimationFrame(() => { d.classList.add("open"); b.classList.add("show"); });
  d.setAttribute("aria-hidden", "false");
  $("menuBtn").setAttribute("aria-expanded", "true");
}

function closeDrawer() {
  drawerOpen = false;
  const d = $("sideDrawer"), b = $("drawerBackdrop");
  d.classList.remove("open");
  b.classList.remove("show");
  d.setAttribute("aria-hidden", "true");
  $("menuBtn").setAttribute("aria-expanded", "false");
  setTimeout(() => { if (!drawerOpen) b.hidden = true; }, 250);
  if (tokenTimer) { clearInterval(tokenTimer); tokenTimer = null; }
}

async function fetchTokens() {
  try {
    const res = await fetch("/api/tokens");
    const data = await res.json();
    if (res.ok) renderTokens(data);
  } catch (e) { /* ignore transient polling errors */ }
}

async function postTokens(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { alert(data.error || "Could not update budgets."); return; }
    lastTokens = data;
    forceRefreshDetail(); // show the change immediately, even if a control had focus
  } catch (e) { alert("Could not reach the server."); }
}

// Passive update (polling): store the latest numbers and re-render the Team page
// only if the user isn't mid-edit (refreshDetailIf guards against that).
function renderTokens(s) {
  lastTokens = s;
  refreshDetailIf(["team"]); // the Token Manager lives on the Team page now
}

// ---------------------------------------------------------------------------
// Tiny, SAFE markdown renderer (escape first, then format)
// ---------------------------------------------------------------------------
function renderMarkdown(md) {
  const lines = esc(md).split("\n");
  let html = "";
  let listType = null; // "ul" | "ol" | null

  const closeList = () => {
    if (listType) {
      html += `</${listType}>`;
      listType = null;
    }
  };

  for (let raw of lines) {
    let line = raw.trimEnd();
    if (/^\s*#{1,6}\s+/.test(line)) {
      closeList();
      const level = line.match(/^\s*(#{1,6})/)[1].length;
      html += `<h${level}>${inline(line.replace(/^\s*#{1,6}\s+/, ""))}</h${level}>`;
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`;
    } else if (/^\s*\d+\.\s+/.test(line)) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inline(line.replace(/^\s*\d+\.\s+/, ""))}</li>`;
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      html += `<p>${inline(line)}</p>`;
    }
  }
  closeList();
  return html;
}

function inline(s) {
  return s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+?)`/g, "<code>$1</code>")
    .replace(/(^|[^*])\*([^*]+?)\*(?!\*)/g, "$1<em>$2</em>");
}

// ---------------------------------------------------------------------------
// Final report (Phase 6): per-section 👍/👎 feedback + the 数据来源与核验 trail
// ---------------------------------------------------------------------------

// Split the report markdown into sections at #/##/### headings. Everything
// before the first heading becomes an opening section. Each entry is
// {title, md}. Section titles drive the feedback rows (feedback.section_title).
function splitReportSections(md) {
  const lines = String(md || "").split("\n");
  const sections = [];
  let cur = { title: "", lines: [] };
  let started = false;
  const flush = () => {
    if (started || cur.lines.some((l) => l.trim())) {
      sections.push({ title: cur.title, md: cur.lines.join("\n") });
    }
  };
  for (const line of lines) {
    const m = /^(#{1,3})\s+(.*)$/.exec(line.trim());
    if (m) {
      flush();
      cur = { title: m[2].trim(), lines: [line] };
      started = true;
    } else {
      cur.lines.push(line);
    }
  }
  flush();
  return sections.length ? sections : [{ title: "", md: String(md || "") }];
}

function computedByCN(v) {
  if (v === "trend_engine") return "趋势引擎（精确计算）";
  if (v === "data_analyst") return "数据分析";
  return v || "—";
}

// The collapsible 数据来源与核验 table, or a "couldn't build it" banner when the
// Report Writer's SOURCES_JSON failed to parse on a report that had anchors.
function sourcesSectionHTML(snap) {
  const st = snap && snap.sources_status;
  const rows = (snap && snap.sources) || [];
  const hasAnchors = /【\s*\d+\s*】/.test((snap && snap.final_report) || "");
  if (st === "ok" && rows.length) {
    const trs = rows.map((s) => `
      <tr>
        <td class="src-anchor">${esc(s.anchor != null ? "【" + s.anchor + "】" : "—")}</td>
        <td>${esc(s.claim_cn || "—")}</td>
        <td>${esc(s.source_file || "—")}</td>
        <td>${esc(s.source_detail || "—")}</td>
        <td>${esc(computedByCN(s.computed_by))}</td>
      </tr>`).join("");
    return `
      <details class="sources-block" open>
        <summary>数据来源与核验（${rows.length}）</summary>
        <div class="sources-table-wrap">
          <table class="sources-table">
            <thead><tr><th>锚点</th><th>数据 / 结论</th><th>来源文件</th><th>定位</th><th>核验方式</th></tr></thead>
            <tbody>${trs}</tbody>
          </table>
        </div>
      </details>`;
  }
  if (st === "malformed" || (st === "missing" && hasAnchors)) {
    return `<div class="sources-banner">本次未能生成数据溯源表，报告中的数字请以正文与原始数据为准。</div>`;
  }
  return "";
}

// One 👍/👎 + optional comment bar under a report section. Only signed-in users
// see it (feedback is tied to their company); run id rides along in a data attr.
function feedbackBarHTML(title, snap) {
  const rid = snap && snap.run_id != null ? String(snap.run_id) : "";
  return `
    <div class="fb-bar" data-section="${esc(title || "报告开头")}" data-run="${esc(rid)}">
      <span class="fb-q">这一节有用吗？</span>
      <button type="button" class="fb-vote fb-up" data-vote="up" title="有用">👍</button>
      <button type="button" class="fb-vote fb-down" data-vote="down" title="需要改进">👎</button>
      <input type="text" class="fb-comment" placeholder="可选：补充说明，例如「以后把退款放第一节」" />
      <button type="button" class="fb-submit">提交</button>
      <span class="fb-msg" hidden></span>
    </div>`;
}

// The full report body: each section rendered, followed by its feedback bar
// (signed-in only), then the verification trail. Used in chatbotDetailHTML.
function reportBodyHTML(snap) {
  const sections = splitReportSections(snap && snap.final_report);
  const showFeedback = !!currentAccount;
  let html = "";
  for (const sec of sections) {
    html += `<div class="report-section">${renderMarkdown(sec.md)}`;
    if (showFeedback) html += feedbackBarHTML(sec.title, snap);
    html += `</div>`;
  }
  html += sourcesSectionHTML(snap);
  return html;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

// Token counts: show compact (1.2M, 950K) so the meters stay readable.
function fmtNum(n) {
  n = Number(n || 0);
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(n % 1_000 === 0 ? 0 : 1) + "K";
  return String(n);
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch (e) {
    return "";
  }
}

// Chinese-Yuan formatter for the whole app — balances, budgets, prices, per-call
// costs (default 2 dp; use 4 for tiny per-call costs). The ¥ symbol comes from
// /api/meta (falls back to "¥"). All money the server sends is already in ¥.
function cny(n, dp = 2) {
  if (n == null || isNaN(n)) return "—";
  const sym = (META && META.currency_symbol) || "¥";
  return `${sym}${Number(n).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
}

// ===========================================================================
// HOME — the hero's two big buttons. "Get API Key" opens the Get Unified API
// workspace; "Explore Models and Pricing" opens the Models & Pricing page.
// ===========================================================================
function wireHome() {
  const getKey = $("heroGetKey");
  const explore = $("heroExplore");
  if (getKey) getKey.addEventListener("click", () => openDetail("unified"));
  if (explore) explore.addEventListener("click", () => openDetail("models"));
}

// ===========================================================================
// FEATURE 1 — Voice input (Web Speech API). Speak your task into the box.
// ===========================================================================
function wireMic() {
  const micBtn = $("micBtn");
  const hint = $("micHint");
  const goalEl = $("goal");
  if (!micBtn || !goalEl) return; // the Chat bot tab isn't mounted right now

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    // Unsupported (e.g. Firefox): hide the mic and explain once.
    micBtn.style.display = "none";
    return;
  }

  recognition = new SR();
  recognition.lang = "en-US";
  recognition.interimResults = true;
  recognition.continuous = true;

  let baseText = ""; // whatever was already typed before this dictation began

  recognition.addEventListener("start", () => {
    recognizing = true;
    baseText = goalEl.value.trim();
    micBtn.classList.add("recording");
    micBtn.title = "Stop recording";
    hint.hidden = false;
    hint.classList.remove("error");
    hint.textContent = "Listening… speak your task, then click the mic to stop.";
  });

  recognition.addEventListener("result", (e) => {
    // Rebuild the full transcript (interim + final) each event — simplest & robust.
    let txt = "";
    for (let i = 0; i < e.results.length; i++) txt += e.results[i][0].transcript;
    const joiner = baseText && !/\s$/.test(baseText) ? " " : "";
    goalEl.value = (baseText + joiner + txt).replace(/^\s+/, "");
    goalDraft = goalEl.value; // keep the remembered draft in sync with dictation
  });

  recognition.addEventListener("end", () => {
    recognizing = false;
    micBtn.classList.remove("recording");
    micBtn.title = "Speak your task";
    if (hint && !hint.classList.contains("error")) hint.hidden = true;
  });

  recognition.addEventListener("error", (e) => {
    recognizing = false;
    micBtn.classList.remove("recording");
    hint.hidden = false;
    hint.classList.add("error");
    hint.textContent = micErrorText(e.error);
  });

  micBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (recognizing) { recognition.stop(); return; }
    try { recognition.start(); }
    catch (err) { /* start() throws if already running — safe to ignore */ }
  });
}

function micErrorText(err) {
  switch (err) {
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access was blocked. Allow the mic for this site, then try again.";
    case "no-speech":
      return "Didn't catch anything — click the mic and try speaking again.";
    case "audio-capture":
      return "No microphone was found. Check your mic and try again.";
    case "network":
      return "The speech service is unreachable. Check your internet and try again.";
    default:
      return "Voice input stopped. Click the mic to try again.";
  }
}

// ===========================================================================
// FEATURE 2 — Detail window: clickable module titles open a full-screen view
// with a Back/Forward history stack and a segmented tab switcher.
// ===========================================================================
function wireDetail() {
  // Module titles in the drawer are the openers.
  document.querySelectorAll(".module-title-btn").forEach((btn) =>
    btn.addEventListener("click", () => openDetail(btn.dataset.view))
  );
  // Tabs inside the window switch views (and push onto the history stack).
  document.querySelectorAll(".detail-tab").forEach((btn) =>
    btn.addEventListener("click", () => navigateDetail(btn.dataset.view))
  );
  $("detailBack").addEventListener("click", detailBack);
  $("detailForward").addEventListener("click", detailForward);
  $("detailClose").addEventListener("click", closeDetail);
}

function openDetail(view) {
  detailHistory = [view]; // start a fresh stack at the chosen view
  detailIndex = 0;
  detailOpen = true;
  const v = $("detailView");
  v.classList.add("open");
  v.setAttribute("aria-hidden", "false");
  fetchTokens(); // the Team view shows the merged Token Manager — keep it fresh
  applyDetailState();
}

function navigateDetail(view) {
  if (detailHistory[detailIndex] === view) return; // already showing it
  detailHistory = detailHistory.slice(0, detailIndex + 1); // drop any forward history
  detailHistory.push(view);
  detailIndex = detailHistory.length - 1;
  applyDetailState();
}

function detailBack() {
  if (detailIndex > 0) { detailIndex--; applyDetailState(); }
}

function detailForward() {
  if (detailIndex < detailHistory.length - 1) { detailIndex++; applyDetailState(); }
}

function applyDetailState() {
  const view = detailHistory[detailIndex];
  // A per-model page (view = "model:<id>") still belongs under the Models tab.
  const tabView = (view && view.indexOf("model:") === 0) ? "models" : view;
  document.querySelectorAll(".detail-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === tabView)
  );
  $("detailBack").disabled = detailIndex <= 0;
  $("detailForward").disabled = detailIndex >= detailHistory.length - 1;
  const body = $("detailBody");
  body.innerHTML = renderDetail(view);
  wireDetailBody();
  body.scrollTop = 0;
  if (view === "team" && !lastTokens) fetchTokens(); // fill the CFO meters on first open
}

function currentView() {
  return detailHistory[detailIndex];
}

function closeDetail() {
  detailOpen = false;
  const v = $("detailView");
  v.classList.remove("open");
  v.setAttribute("aria-hidden", "true");
}

// Re-render the open detail window in place when fresh data arrives — but only
// if the data that changed matches the view we're looking at (keeps it live).
function refreshDetailIf(views) {
  if (!detailOpen) return;
  const view = currentView();
  if (!views.includes(view)) return;
  const body = $("detailBody");
  // Don't re-render out from under a control the user is actively using
  // (an open model dropdown, a budget field mid-type) — wait until they're done.
  const ae = document.activeElement;
  if (ae && body.contains(ae) && /^(INPUT|SELECT|TEXTAREA)$/.test(ae.tagName)) return;
  const top = body.scrollTop;
  body.innerHTML = renderDetail(view);
  wireDetailBody(); // scrolls the Chat bot live feed to the bottom if present
  body.scrollTop = top; // keep the reader's place as the meters/feed tick up
}

// A forced re-render after a user action (model change, budget edit) — ignores
// the focus guard because the action is already done, then keeps the scroll spot.
function forceRefreshDetail() {
  if (!detailOpen) return;
  const view = currentView();
  const body = $("detailBody");
  const top = body.scrollTop;
  body.innerHTML = renderDetail(view);
  wireDetailBody();
  body.scrollTop = top;
}

// (Re)attach listeners to the interactive controls inside the detail body.
// Called after every render because innerHTML replaces the nodes each time.
function wireDetailBody() {
  const body = $("detailBody");
  if (!body) return;

  // --- Chat bot tab: the task form (goal + mic + files + run button) ---
  const goalBox = body.querySelector("#goal");
  if (goalBox) {
    goalBox.value = goalDraft; // restore a typed-but-unsent goal across re-renders
    goalBox.addEventListener("input", () => { goalDraft = goalBox.value; });
    const indSel = body.querySelector("#industrySelect");
    if (indSel) {
      indSel.value = industryDraft; // restore the industry pick across re-renders
      indSel.addEventListener("change", () => { industryDraft = indSel.value; });
    }
    wireFileInputs();   // browse + drag-drop (looks up #dropZone/#fileInput itself)
    renderFileList();   // re-draw any already-attached files
    wireMic();          // voice dictation into #goal
    const run = body.querySelector("#runBtn");
    if (run) run.addEventListener("click", startJob);
    setRunning(jobRunning); // reflect an in-flight job on the button label
    // The live progress feed lives in this same tab — keep it pinned to the newest line.
    const chatFeed = body.querySelector("#chatFeed");
    if (chatFeed) chatFeed.scrollTop = chatFeed.scrollHeight;
    // Copy the final report (shown once the job is done).
    const chatCopy = body.querySelector("#chatCopyBtn");
    if (chatCopy) chatCopy.addEventListener("click", () => {
      navigator.clipboard.writeText(lastReport || "").then(() => {
        chatCopy.textContent = "Copied!";
        setTimeout(() => (chatCopy.textContent = "Copy"), 1500);
      });
    });
    wireFeedbackBars(body);  // per-section 👍/👎 on the final report
  }

  // Per-agent model dropdowns (Team page).
  body.querySelectorAll(".model-select").forEach((sel) =>
    sel.addEventListener("change", () => setAgentModel(sel.dataset.agent, sel.value, sel))
  );

  // Token Manager — set overall total.
  const totalForm = body.querySelector("#dTotalForm");
  if (totalForm) totalForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = body.querySelector("#dTotalInput").value.trim();
    await postTokens("/api/tokens/total", { limit: v === "" ? 0 : Number(v) });
  });

  // Token Manager — add / update a task budget.
  const budgetForm = body.querySelector("#dBudgetForm");
  if (budgetForm) budgetForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = body.querySelector("#dBudgetName").value.trim();
    const limit = body.querySelector("#dBudgetLimit").value.trim();
    if (!name) return;
    await postTokens("/api/tokens/budget", { name, limit: limit === "" ? 0 : Number(limit) });
  });

  // Token Manager — remove a task budget.
  body.querySelectorAll(".budget-rm").forEach((btn) =>
    btn.addEventListener("click", () => {
      if (confirm(`Remove the "${btn.dataset.name}" budget?`))
        postTokens("/api/tokens/budget/remove", { name: btn.dataset.name });
    })
  );

  // Token Manager — reset usage counters.
  const reset = body.querySelector("#dResetTokens");
  if (reset) reset.addEventListener("click", () => {
    if (confirm("Reset all spend counters to ¥0? (Your budgets are kept.)"))
      postTokens("/api/tokens/reset", {});
  });

  // --- Unified API dashboard: the editable model picker (signed-in only) ---
  // Ticking a box updates the in-memory set + count in place (no re-render, so the
  // page doesn't jump). "Save" POSTs the set to /api/account/models.
  body.querySelectorAll(".dash-check").forEach((cb) =>
    cb.addEventListener("change", () => {
      if (cb.checked) dashSelected.add(cb.value); else dashSelected.delete(cb.value);
      const item = cb.closest(".shelf-item");
      if (item) item.classList.toggle("on", cb.checked);
      const count = body.querySelector("#dashModelCount");
      if (count) count.textContent = String(dashSelected.size);
    })
  );

  // "select all" for a provider group (text models only).
  body.querySelectorAll(".dash-group-all").forEach((btn) =>
    btn.addEventListener("click", () => {
      MODEL_CHOICES
        .filter((m) => (m.provider || "anthropic") === btn.dataset.provider && isTextModel(m))
        .forEach((m) => dashSelected.add(m.id));
      forceRefreshDetail();
    })
  );

  // Save the chosen models onto the signed-in account's key.
  const dashSave = body.querySelector("#dashSaveModels");
  if (dashSave) dashSave.addEventListener("click", saveDashModels);

  // --- Unified API dashboard: refresh the signed-in account (session-based) ---
  const dashRefresh = body.querySelector("#dashRefresh");
  if (dashRefresh) dashRefresh.addEventListener("click", refreshAccount);

  // Storefront prompts shown when nobody is signed into this browser yet.
  const promptLogin = body.querySelector("#unifiedLogin");
  if (promptLogin) promptLogin.addEventListener("click", openLoginModal);
  const promptSignup = body.querySelector("#unifiedSignup");
  if (promptSignup) promptSignup.addEventListener("click", openSignup);

  // --- Dashboard: top up the account balance (¥, via a real payment order) ---
  const topForm = body.querySelector("#topupForm");
  if (topForm) topForm.addEventListener("submit", (e) => { e.preventDefault(); doTopup(); });
  body.querySelectorAll(".topup-quick:not(.plan-buy)").forEach((b) =>
    b.addEventListener("click", () => doTopup(Number(b.dataset.amt)))
  );

  // --- Dashboard: subscription plans + orders/fapiao (Phase 9, async fill) ---
  if (body.querySelector("#planBox") || body.querySelector("#ordersBox")) {
    loadBillingPanels(body);
  }

  // --- Models & Pricing: function tabs + clickable model cards ---
  body.querySelectorAll(".fn-tab").forEach((btn) =>
    btn.addEventListener("click", () => { modelsKind = btn.dataset.kind; forceRefreshDetail(); })
  );
  body.querySelectorAll(".model-card[data-model]").forEach((card) => {
    const open = () => navigateDetail("model:" + card.dataset.model);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
  });
  const mdBack = body.querySelector("#mdBack");
  if (mdBack) mdBack.addEventListener("click", () => navigateDetail("models"));

  // --- Fusion tab ---
  wireFusionBody(body);

  // --- Company Brain page (企业大脑) ---
  wireBrainBody(body);

  // --- UNKNOWN_TERMS confirm banner (on the Chat bot result page) ---
  wireConfirmTerms(body);
}

// Wire the Fusion tab's controls (presets, model chips, fuser, prompt, run).
function wireFusionBody(body) {
  // Preset chips (Quality / Budget / Custom).
  body.querySelectorAll(".fusion-preset").forEach((btn) =>
    btn.addEventListener("click", () => {
      fusionPreset = btn.dataset.preset;
      if (fusionPreset === "quality") {
        fusionSelected = presetModels(["claude-opus-4-7", "gpt-5.1", "gemini-2.5-pro"]);
      } else if (fusionPreset === "budget") {
        fusionSelected = presetModels(["claude-haiku-4-5", "gpt-5-mini", "gemini-2.5-flash"]);
      } // "custom" leaves the current selection alone
      if (fusionFuser !== "auto" && !fusionSelected.includes(fusionFuser)) fusionFuser = "auto";
      forceRefreshDetail();
    })
  );

  // Remove a model chip from the council.
  body.querySelectorAll(".fc-rm").forEach((btn) =>
    btn.addEventListener("click", () => {
      fusionSelected = fusionSelected.filter((id) => id !== btn.dataset.id);
      fusionPreset = "custom";
      if (fusionFuser === btn.dataset.id) fusionFuser = "auto";
      forceRefreshDetail();
    })
  );

  // Add a model (dropdown of text models not already chosen).
  const fusionAdd = body.querySelector("#fusionAdd");
  if (fusionAdd) fusionAdd.addEventListener("change", () => {
    const id = fusionAdd.value;
    if (id && !fusionSelected.includes(id) && fusionSelected.length < 5) {
      fusionSelected.push(id);
      fusionPreset = "custom";
    }
    forceRefreshDetail();
  });

  // Which model fuses the answers.
  const fuserSel = body.querySelector("#fusionFuser");
  if (fuserSel) fuserSel.addEventListener("change", () => { fusionFuser = fuserSel.value; });

  // Keep the typed prompt across re-renders.
  const promptEl = body.querySelector("#fusionPrompt");
  if (promptEl) promptEl.addEventListener("input", () => { fusionPrompt = promptEl.value; });

  // Run the fusion.
  const runBtn = body.querySelector("#fusionRun");
  if (runBtn) runBtn.addEventListener("click", runFusion);

  // Copy the fused answer.
  const fusionCopy = body.querySelector("#fusionCopy");
  if (fusionCopy) fusionCopy.addEventListener("click", () => {
    const txt = (fusionResult && fusionResult.fused && fusionResult.fused.text) || "";
    navigator.clipboard.writeText(txt).then(() => {
      fusionCopy.textContent = "Copied!";
      setTimeout(() => (fusionCopy.textContent = "Copy"), 1500);
    });
  });
}

function renderDetail(view) {
  if (view === "chatbot") return chatbotDetailHTML();
  if (view === "fusion") return fusionDetailHTML();
  if (view === "team") return teamDetailHTML();
  if (view === "models") return modelsDetailHTML();
  if (view && view.indexOf("model:") === 0) return modelDetailHTML(view.slice(6));
  if (view === "unified") return unifiedApiDetailHTML();
  if (view === "brain") return brainDetailHTML();
  return "";
}

// ---------------------------------------------------------------------------
// Small catalog helpers (shared by Fusion + the dashboard model editor)
// ---------------------------------------------------------------------------
function isTextModel(m) { return (m && (m.kind || "text")) === "text"; }
function modelById(id) { return MODEL_CHOICES.find((m) => m.id === id) || null; }
// Only "text" models are callable (chat gateway, Fusion, teammates).
function textModels() { return MODEL_CHOICES.filter(isTextModel); }
// Keep only catalog text-model ids (drops unknowns + non-text models), order preserved.
function presetModels(ids) {
  return ids.filter((id) => { const m = modelById(id); return m && isTextModel(m); });
}

// Model-function metadata from /api/meta (the KINDS dict + its order).
function kindMeta(k) { return (META && META.kinds && META.kinds[k]) || null; }
function kindLabel(k) {
  const km = kindMeta(k);
  return (km && km.label) || (k ? k.charAt(0).toUpperCase() + k.slice(1) : "—");
}
// The function tabs to show on the Models page: the configured order, limited to
// kinds that actually have at least one model (falls back to a single Text tab).
function modelKinds() {
  const order = (META && META.kind_order) || ["text"];
  const present = new Set(MODEL_CHOICES.map((m) => m.kind || "text"));
  const ks = order.filter((k) => present.has(k));
  return ks.length ? ks : ["text"];
}

// Chat bot — the task form PLUS the live progress feed + final report, all in one
// tab (the old Live Activity page is gone). Everything below the form renders from
// lastSnapshot, so each poll just re-renders this tab. Wired in wireDetailBody().
function chatbotDetailHTML() {
  const snap = lastSnapshot;
  const status = snap ? snap.status : null;

  // Live progress feed (rendered fully from the snapshot on every poll).
  let progress = "";
  const acts = (snap && snap.activity) || [];
  if (acts.length) {
    progress = `
      <div class="detail-section">
        <div class="block-title">Live activity ${statusPillHTML(status)}</div>
        <ul class="feed detail-feed" id="chatFeed">${acts.map(feedRowHTML).join("")}</ul>
      </div>`;
  } else if (jobRunning) {
    progress = `
      <div class="detail-section">
        <div class="block-title">Live activity ${statusPillHTML(status || "starting")}</div>
        <p class="muted">Starting the team…</p>
      </div>`;
  }

  // Final report + any downloadable files the team produced.
  let report = "";
  if (snap && snap.status === "done" && snap.final_report) {
    report = `
      <div class="detail-section">
        <div class="report-head">
          <div class="block-title" style="margin:0;">Final deliverable</div>
          <button id="chatCopyBtn" class="copy-btn">Copy</button>
        </div>
        ${deliverablesHTML(snap.deliverables || [])}
        <div class="report-body">${reportBodyHTML(snap)}</div>
      </div>`;
  }

  // A transport error (couldn't reach the server) or an in-band team error.
  const errMsg = jobError
    || (snap && snap.status === "error" ? ("The team hit an error: " + (snap.error || "unknown")) : "");
  const errLine = errMsg ? `<p class="form-error" style="display:block">${esc(errMsg)}</p>` : "";

  // The learning loop: if the Data Analyst flagged terms it couldn't interpret,
  // show a yellow "confirm these terms" banner with one question per term. Hidden
  // once the user confirms/dismisses for this job (tracked by job_id).
  const banner = confirmTermsBannerHTML(snap);

  return `
    <h2 class="detail-h">Chat bot</h2>
    <p class="detail-lead">Describe what you want done and (optionally) attach files. The CEO agent plans the work, delegates to the specialists, and you watch it all happen right here.</p>
    <div class="detail-section">
      <div class="goal-wrap">
        <textarea id="goal" class="goal" rows="5"
          placeholder="e.g. Research our top 3 competitors and draft a one-page positioning brief, then turn it into a slide."></textarea>
        <button id="micBtn" class="mic-btn" type="button" title="Speak your task" aria-label="Speak your task">
          <span class="mic-ico" aria-hidden="true"></span>
        </button>
      </div>
      <p id="micHint" class="mic-hint" hidden></p>

      <div class="industry-row">
        <label for="industrySelect" class="industry-label">行业模板</label>
        <select id="industrySelect" class="industry-select" aria-label="行业模板">
          <option value="auto">自动 / 按公司档案</option>
          ${INDUSTRIES.map((p) => `<option value="${esc(p.key)}">${esc(p.display_name_cn)}</option>`).join("")}
        </select>
      </div>

      <div id="dropZone" class="dropzone">
        <p class="dz-main">Drag &amp; drop files here</p>
        <p class="dz-sub">or <button id="browseBtn" type="button" class="link-btn">browse your computer</button></p>
        <input id="fileInput" type="file" multiple hidden />
      </div>
      <ul id="fileList" class="file-list"></ul>

      <button id="runBtn" class="run-btn" type="button">Start the team</button>
      <p id="formError" class="form-error" hidden></p>
    </div>
    ${errLine}
    ${banner}
    ${progress}
    ${report}`;
}

function teamDetailHTML() {
  const snap = lastSnapshot;
  if (!AGENTS.length) {
    return `<h2 class="detail-h">Your team</h2><p class="muted">The team hasn't loaded yet.</p>`;
  }
  const ceo = AGENTS.find((a) => a.reports_to === null);
  const modelOptions = (selectedId) =>
    MODEL_CHOICES.map((m) =>
      `<option value="${esc(m.id)}" ${m.id === selectedId ? "selected" : ""}>${esc(m.label)}</option>`
    ).join("");
  const card = (a) => {
    const cls = stateClass(a.id, snap);
    const isCeo = !!(ceo && a.id === ceo.id);
    const stateChip =
      cls === "done" ? `<span class="chip state-done">Done</span>` :
      cls === "active" ? `<span class="chip state-active">Working…</span>` :
      `<span class="chip">Idle</span>`;
    const reports = isCeo
      ? "Leads the team"
      : `Reports to ${esc(whoName(a.reports_to))}`;
    // Each card carries its own model dropdown; if the choices haven't loaded,
    // fall back to a plain read-only label so the card still renders.
    const modelControl = MODEL_CHOICES.length
      ? `<select class="model-select" data-agent="${esc(a.id)}" aria-label="Model for ${esc(a.name)}">${modelOptions(a.model)}</select>`
      : `<span class="chip model">${esc(modelLabel(a.model))}</span>`;
    return `<div class="agent-card ${isCeo ? "ceo" : ""} ${cls}">
      <div class="agent-card-top">
        <span class="node-dot"></span>
        <span class="agent-name">${esc(a.name)}</span>
      </div>
      <div class="agent-role">${esc(a.title)}</div>
      <div class="agent-meta">
        ${modelControl}
        ${stateChip}
      </div>
      <div class="agent-reports">${reports}</div>
    </div>`;
  };
  const workers = AGENTS.filter((a) => a.reports_to !== null);
  return `
    <h2 class="detail-h">Your team</h2>
    <p class="detail-lead">${AGENTS.length} teammates, organized like a company. Pick each one's model from its card — changes apply to your next job.</p>
    ${ceo ? `<div class="detail-section"><div class="block-title">Leadership</div><div class="agent-grid agent-grid-ceo">${card(ceo)}</div></div>` : ""}
    <div class="detail-section">
      <div class="block-title">Specialists</div>
      <div class="agent-grid">${workers.map(card).join("")}</div>
    </div>
    ${tokensSectionHTML()}`;
}

// ===========================================================================
// FUSION — run one prompt across several models at once, then fuse them into one
// best, fact-checked answer. Where one model is wrong, the others catch it. Uses
// the server's own provider keys (like the Chat bot), not a customer gateway key.
// ===========================================================================
function fusionDetailHTML() {
  if (!MODEL_CHOICES.length) {
    return `<h2 class="detail-h">Fusion <span class="beta-badge">beta</span></h2>
      <p class="muted">The model catalog hasn't loaded yet.</p>`;
  }
  // First open: seed the council with the first three text models (Opus/Sonnet/Haiku).
  if (!fusionSelected.length) fusionSelected = textModels().slice(0, 3).map((m) => m.id);

  const chips = fusionSelected.map((id) => {
    const m = modelById(id);
    return `<span class="fusion-chip">
      <span class="fc-label">${esc(m ? m.label : id)}</span>
      <button type="button" class="fc-rm" data-id="${esc(id)}" title="Remove" aria-label="Remove">&times;</button>
    </span>`;
  }).join("");

  // "+ Add model" dropdown (text models not already in the council; capped at 5).
  const addable = textModels().filter((m) => !fusionSelected.includes(m.id));
  let addControl;
  if (fusionSelected.length >= 5) {
    addControl = `<span class="fusion-addfull">Max 5 models</span>`;
  } else if (!addable.length) {
    addControl = `<span class="fusion-addfull">All models added</span>`;
  } else {
    const opts = addable.map((m) =>
      `<option value="${esc(m.id)}">${esc(m.label)} · ${esc(providerLabel(m.provider))}</option>`).join("");
    addControl = `<select id="fusionAdd" class="fusion-add"><option value="">+ Add model</option>${opts}</select>`;
  }

  // "Fuse with" selector: Auto, or any one of the chosen models.
  const fuserOpts = [`<option value="auto" ${fusionFuser === "auto" ? "selected" : ""}>Auto (best available)</option>`]
    .concat(fusionSelected.map((id) => {
      const m = modelById(id);
      return `<option value="${esc(id)}" ${fusionFuser === id ? "selected" : ""}>${esc(m ? m.label : id)}</option>`;
    })).join("");

  const presetChip = (key, label) =>
    `<button type="button" class="fusion-preset ${fusionPreset === key ? "on" : ""}" data-preset="${key}">${label}</button>`;

  const err = fusionError ? `<p class="form-error" style="display:block">${esc(fusionError)}</p>` : "";
  const results = fusionRunning
    ? `<div class="fusion-running"><span class="spinner"></span> Running ${fusionSelected.length} model${fusionSelected.length === 1 ? "" : "s"} &amp; fusing the best answer…</div>`
    : (fusionResult ? fusionResultsHTML(fusionResult) : "");

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
        <textarea id="fusionPrompt" class="fusion-prompt" rows="4" placeholder="Ask anything — every selected model answers, then they're fused into one best result.">${esc(fusionPrompt)}</textarea>
        <button id="fusionRun" class="run-btn" type="button" ${fusionRunning ? "disabled" : ""}>${fusionRunning ? "Running…" : "Run fusion"}</button>
      </div>
      ${err}
    </div>
    ${results}`;
}

// Render the fused answer (highlighted) plus each model's individual answer.
function fusionResultsHTML(r) {
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

  // This run was billed to the logged-in account; show what it cost + what's left.
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

// POST the prompt + chosen models to the fusion endpoint and show the result.
async function runFusion() {
  const body = $("detailBody");
  const promptEl = body && body.querySelector("#fusionPrompt");
  if (promptEl) fusionPrompt = promptEl.value;
  const prompt = (fusionPrompt || "").trim();
  fusionError = "";
  if (!prompt) { fusionError = "Type a question or task to run."; forceRefreshDetail(); return; }
  if (!fusionSelected.length) { fusionError = "Pick at least one model."; forceRefreshDetail(); return; }

  fusionRunning = true;
  fusionResult = null;
  forceRefreshDetail();
  try {
    const res = await fetch("/api/fusion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, models: fusionSelected, fuser: fusionFuser }),
    });
    const data = await res.json();
    if (!res.ok) {
      // Fusion is billed to the logged-in account too: 401 = log in, 402 = top up.
      fusionError = data.error || "Fusion failed.";
      if (res.status === 401) openLoginModal();
      return; // the finally below clears the spinner + re-renders
    }
    fusionResult = data;
  } catch (e) {
    fusionError = e.message;
  } finally {
    fusionRunning = false;
    forceRefreshDetail();
  }
}

// Models & Pricing — every model the team can run on, grouped by provider, with
// list pricing. Driven entirely by MODEL_CHOICES + PROVIDERS (served from the
// API), so adding a model/provider in config.py makes it appear here automatically.
function modelsDetailHTML() {
  if (!MODEL_CHOICES.length) {
    return `<h2 class="detail-h">Models &amp; Pricing</h2>
      <p class="muted">The model list hasn't loaded yet.</p>`;
  }
  // Function tabs (Text / Image / Embeddings / …) — only kinds that have models.
  const kinds = modelKinds();
  if (!kinds.includes(modelsKind)) modelsKind = kinds[0] || "text";
  const tabs = kinds.map((k) => {
    const n = MODEL_CHOICES.filter((m) => (m.kind || "text") === k).length;
    return `<button type="button" class="fn-tab ${k === modelsKind ? "on" : ""}" data-kind="${esc(k)}">
      ${esc(kindLabel(k))}<span class="fn-tab-n">${n}</span></button>`;
  }).join("");

  const items = MODEL_CHOICES.filter((m) => (m.kind || "text") === modelsKind);
  const sections = modelsByProvider(items).map((g) => `
    <div class="provider-group">
      <div class="provider-head">
        <span class="provider-name">${esc(g.label)}</span>
        <span class="provider-count">${g.items.length} model${g.items.length === 1 ? "" : "s"}</span>
      </div>
      <div class="model-grid">${g.items.map(modelCardHTML).join("")}</div>
    </div>`).join("");

  const km = kindMeta(modelsKind);
  const lead = (km && km.callable === false)
    ? `${esc(kindLabel(modelsKind))} models are catalog entries priced ${esc(km.unit || "per unit")} — browse their specs here. The chat gateway &amp; Fusion call <strong>Text</strong> models only.`
    : `Text models power the chat gateway, Fusion, and every teammate. Click any model for its full spec sheet.`;
  const rate = META ? `≈¥${META.usd_to_cny}/US$` : "the configured rate";

  return `
    <h2 class="detail-h">Models &amp; Pricing</h2>
    <p class="detail-lead">${MODEL_CHOICES.length} models across ${Object.keys(PROVIDERS).length || "several"} providers, organized by what they do. Click any model for its full spec sheet — prices shown in ¥.</p>
    <div class="fn-tabs">${tabs}</div>
    <p class="fn-lead">${lead}</p>
    ${sections}
    <p class="models-foot">Claude models run natively on the official Anthropic API (exact pricing). Every other provider runs through one OpenAI-compatible adapter; prices &amp; ids are best-effort public values (marked “approx”) — confirm with each provider. Prices are each provider's list price converted to ¥ (${rate}). Add a provider's key to your <code>.env</code> to activate its models.</p>`;
}

// One model card on the Models page — the whole card is clickable (opens the
// model's detail page). Price block adapts to the model's kind.
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

// The ¥ price block for a model, shaped by its kind: text models show input +
// output per-1M-tokens; everything else shows a single price per its own unit.
function modelPriceHTML(m) {
  const kind = m.kind || "text";
  if (kind === "text") {
    return `<div class="mc-prices">
        <div class="mc-price"><span class="mc-price-val">${cny(m.input_per_mtok_cny, 2)}</span><span class="mc-price-label">input / 1M tokens</span></div>
        <div class="mc-price"><span class="mc-price-val">${cny(m.output_per_mtok_cny, 2)}</span><span class="mc-price-label">output / 1M tokens</span></div>
      </div>`;
  }
  const unit = m.price_unit || (kindMeta(kind) && kindMeta(kind).unit) || "per unit";
  const dp = (m.price_cny != null && m.price_cny < 1) ? 4 : 2; // tiny per-unit prices need precision
  return `<div class="mc-prices one">
      <div class="mc-price"><span class="mc-price-val">${cny(m.price_cny, dp)}</span><span class="mc-price-label">${esc(unit)}</span></div>
    </div>`;
}

// Per-model detail page (route view = "model:<id>") — an OpenRouter-style spec
// sheet: pricing, provider, function, modalities, context window, availability.
function modelDetailHTML(id) {
  const m = modelById(id);
  if (!m) {
    return `<button type="button" class="md-back" id="mdBack">&lsaquo; All models</button>
      <h2 class="detail-h">Model not found</h2>
      <p class="muted">That model isn't in the catalog anymore.</p>`;
  }
  const kind = m.kind || "text";
  const km = kindMeta(kind);
  const modalities = km ? `${esc(km.input)} &rarr; ${esc(km.output)}` : "—";
  const availability = m.available
    ? `<span class="md-dot live"></span> Live now — this provider's key is configured`
    : `<span class="md-dot"></span> Needs <code>${esc((m.provider || "this provider"))}</code>'s key in <code>.env</code>`;
  const access = (km && km.callable)
    ? `Callable from the chat gateway, Fusion &amp; agent teammates`
    : `Catalog model — not callable from the text chat gateway`;
  const spec = (label, val) => val
    ? `<div class="md-spec"><span class="md-k">${esc(label)}</span><span class="md-v">${val}</span></div>` : "";
  return `
    <button type="button" class="md-back" id="mdBack">&lsaquo; All models</button>
    <div class="md-head">
      <div>
        <h2 class="detail-h" style="margin:0 0 6px;">${esc(m.label)} ${m.approx ? `<span class="mc-approx">approx</span>` : ""}</h2>
        <code class="md-id">${esc(m.id)}</code>
      </div>
      <span class="mc-tier">${esc(m.tier || kindLabel(kind))}</span>
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
        ${spec("Function", esc(kindLabel(kind)))}
        ${spec("Modalities", modalities)}
        ${spec("Context window", (m.context && m.context !== "—") ? esc(m.context) : "")}
        ${spec("Availability", availability)}
        ${spec("Access", access)}
      </div>
    </div>`;
}

// ===========================================================================
// GET UNIFIED API — the logged-in dashboard. Model selection is gated behind an
// account now: signed-out visitors see only a "create account / log in" CTA;
// signed-in users get an editable model picker (tick the models their ONE
// OpenAI-compatible key unlocks), their balance, and recent usage. Powered by
// /api/me, /api/account/models, and the /v1/chat/completions gateway.
// ===========================================================================
function unifiedApiDetailHTML() {
  // Signed in → this is your live dashboard (models editor, balance, usage). Model
  // selection lives ONLY here now — gated behind a logged-in account.
  if (currentAccount) {
    // Seed the dashboard's model editor from the account's saved models, but only
    // ONCE per account: a "select all" or save triggers forceRefreshDetail(), and we
    // must not wipe in-progress (unsaved) ticks every time this re-renders.
    if (dashSeededFor !== currentAccount.id) {
      dashSelected = new Set((currentAccount.models || []).map((m) => m.id));
      dashSeededFor = currentAccount.id;
    }
    const dashErr = accountError
      ? `<p class="form-error" style="display:block">${esc(accountError)}</p>` : "";
    return `
      <h2 class="detail-h">Get Unified API</h2>
      <p class="detail-lead">Welcome back, ${esc(currentAccount.name)} — this is your live dashboard. Pick the models you want below, then point any OpenAI-compatible client at <code>${esc(location.origin)}/v1</code> using your key (the one shown once at sign-up) to call every model you've enabled.</p>
      ${dashErr}
      ${dashboardHTML(currentAccount)}`;
  }

  // Signed out → model selection is gated. Show ONLY a prompt to create an account
  // or log in (there is no anonymous storefront / model picker anymore — you pick
  // your models from the dashboard AFTER you sign in).
  return `
    <h2 class="detail-h">Get Unified API</h2>
    <p class="detail-lead">Your own model superstore. Create an account to bundle any models across ${Object.keys(PROVIDERS).length || 11} providers into ONE API key — a single OpenAI-compatible endpoint, the same idea as OpenRouter. The server holds the real provider keys; your key just gates access &amp; meters usage.</p>

    <div class="detail-section signin-cta">
      <div class="block-title">Create your unified API key</div>
      <p class="section-hint">Sign up to get a key <em>and</em> a saved dashboard you can log back into anytime with email + password — or log in if you already have one. You choose which models your key unlocks from your dashboard, right after you sign in.</p>
      <div class="cta-row">
        <button type="button" id="unifiedSignup" class="run-btn">Create an account</button>
        <button type="button" id="unifiedLogin" class="ghost-btn">Log in</button>
      </div>
    </div>`;
}

// The one-time reveal of a freshly minted key, with a copy button + a ready-to-run
// OpenAI-compatible example. Shown right after "Generate my API key".
function keyRevealHTML(key, customer) {
  const theKey = key || "";
  const c = customer || {};
  const base = location.origin + "/v1";
  const ids = (c.models || []).map((m) => m.id);
  const example =
`curl ${base}/chat/completions \\
  -H "Authorization: Bearer ${theKey}" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${ids[0] || "claude-opus-4-7"}", "messages": [{"role": "user", "content": "Hello!"}]}'`;
  return `<div class="key-reveal">
    <div class="kr-title">Your API key is ready — copy it now. For your security it won't be shown again.</div>
    <div class="kr-key"><code id="krKey">${esc(theKey)}</code><button type="button" id="krCopy" class="copy-btn">Copy</button></div>
    <div class="kr-grid">
      <div><span class="kr-k">Base URL</span><code>${esc(base)}</code></div>
      <div><span class="kr-k">Models</span><span>${ids.length} unlocked</span></div>
      <div><span class="kr-k">Balance</span><span>${cny(c.balance)}</span></div>
    </div>
    <div class="kr-curl-label">Try it — drop-in OpenAI-compatible:</div>
    <pre class="kr-curl">${esc(example)}</pre>
  </div>`;
}

// The customer dashboard: balance, lifetime stats, the models on this key (with a
// live/needs-key dot), and a recent-usage table. Built from /api/account.
function dashboardHTML(v) {
  const bal = v.balance == null ? "Unlimited" : cny(v.balance);
  const balLow = (v.balance != null && v.balance <= 0) ? " low" : "";

  // --- The editable model picker (text models only) -------------------------
  // Only text models are callable through the OpenAI-compatible chat gateway, so
  // non-text models (image/embedding/speech/…) are excluded from the key. Reflects
  // dashSelected (seeded from the account in unifiedApiDetailHTML, then edited in place).
  const shelf = (m) => {
    const on = dashSelected.has(m.id);
    const live = m.available ? "live" : "";
    const tip = m.available ? "Live now" : "Add this provider's key to .env to activate";
    const approx = m.approx ? `<span class="mc-approx">approx</span>` : "";
    return `<label class="shelf-item ${on ? "on" : ""}" title="${esc(tip)}">
      <input type="checkbox" class="dash-check" value="${esc(m.id)}" ${on ? "checked" : ""} />
      <span class="shelf-main">
        <span class="shelf-name"><span class="live-dot ${live}"></span>${esc(m.label)} ${approx}</span>
        <span class="shelf-id">${esc(m.id)}</span>
      </span>
      <span class="shelf-price">${cny(m.output_per_mtok_cny)}<span class="shelf-price-label">/1M out</span></span>
    </label>`;
  };
  const groups = modelsByProvider()
    .map((g) => {
      const items = g.items.filter(isTextModel);
      if (!items.length) return "";
      return `<div class="provider-group">
        <div class="provider-head">
          <span class="provider-name">${esc(g.label)}</span>
          <button type="button" class="dash-group-all" data-provider="${esc(g.provider)}">select all</button>
        </div>
        <div class="shelf-grid">${items.map(shelf).join("")}</div>
      </div>`;
    }).join("");
  const editor = groups || `<p class="muted">The model catalog hasn't loaded yet.</p>`;

  // --- Recent usage ---------------------------------------------------------
  const usage = (v.usage || []).slice(0, 12);
  const rows = usage.length
    ? usage.map((u) => `<tr>
        <td>${fmtTime(u.at)}</td>
        <td><code>${esc(u.model)}</code></td>
        <td>${fmtNum(u.input_tokens)} / ${fmtNum(u.output_tokens)}</td>
        <td>${cny(u.cost, 4)}</td>
      </tr>`).join("")
    : `<tr><td colspan="4" class="muted">No calls yet — point any OpenAI client at ${esc(location.origin)}/v1 with this key.</td></tr>`;

  return `<div class="dash">
    <div class="dash-head">
      <div>
        <div class="dash-name">${esc(v.name)}</div>
        <div class="dash-key">${esc(v.key_prefix)}…${esc(v.key_last4)}</div>
      </div>
      <div class="dash-balance">
        <span class="dash-bal-val${balLow}">${bal}</span>
        <span class="dash-bal-label">balance left</span>
      </div>
    </div>
    <div class="dash-stats">
      <div class="dash-stat"><span class="ds-val">${v.requests}</span><span class="ds-label">requests</span></div>
      <div class="dash-stat"><span class="ds-val">${cny(v.spent, 2)}</span><span class="ds-label">spent</span></div>
      <div class="dash-stat"><span class="ds-val" id="dashModelsStat">${(v.models || []).length}</span><span class="ds-label">models on key</span></div>
      <button id="dashRefresh" type="button" class="dash-refresh">Refresh</button>
    </div>

    <div class="dash-topup">
      <span class="dash-topup-label">充值 Top up（支付宝 / 微信支付）</span>
      <div class="dash-topup-row">
        <button type="button" class="topup-quick" data-amt="50">+ ¥50</button>
        <button type="button" class="topup-quick" data-amt="100">+ ¥100</button>
        <button type="button" class="topup-quick" data-amt="200">+ ¥200</button>
        <form id="topupForm" class="inline-form topup-form">
          <input type="number" id="topupAmt" min="1" step="10" placeholder="custom ¥ amount" />
          <button type="submit">去支付</button>
        </form>
      </div>
      <p id="topupMsg" class="dash-save-msg" hidden></p>
      <div id="payPanel" hidden style="margin-top:10px;padding:12px;border:1px solid #d8e0dc;border-radius:10px;background:#fff;"></div>
    </div>

    <div class="block-title" style="margin-top:22px;">订阅套餐 Subscription</div>
    <div id="planBox"><p class="muted">套餐信息加载中…</p></div>

    <div class="block-title" style="margin-top:22px;">订单与发票 Orders &amp; Fapiao</div>
    <div id="ordersBox"><p class="muted">订单加载中…</p></div>

    <div class="block-title" style="margin-top:22px;">Your models</div>
    <p class="section-hint">Tick the models this key can call, then Save. The dot shows whether each provider's key is configured on the server (green = live now).</p>
    <div class="dash-editor">${editor}</div>
    <div class="dash-save-bar">
      <div class="store-summary"><span id="dashModelCount">${dashSelected.size}</span> model${dashSelected.size === 1 ? "" : "s"} selected</div>
      <button type="button" id="dashSaveModels" class="run-btn">Save my models</button>
    </div>
    <p id="dashModelsMsg" class="dash-save-msg" hidden></p>

    <div class="block-title" style="margin-top:22px;">Recent usage</div>
    <table class="dash-usage">
      <thead><tr><th>When</th><th>Model</th><th>In / Out</th><th>Cost</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

// DASHBOARD — save the ticked models onto the signed-in account's key. Model
// selection is gated behind the session (no anonymous storefront anymore): this
// POSTs the chosen ids to /api/account/models, which returns the updated account
// view (each model enriched with an `available` flag). We update in place — the
// checkboxes + "selected" count already reflect dashSelected, so only the "models
// on key" stat needs refreshing — which keeps the success message on screen.
async function saveDashModels() {
  const body = $("detailBody");
  if (!body) return;
  const btn = body.querySelector("#dashSaveModels");
  const msg = body.querySelector("#dashModelsMsg");
  const show = (text, ok) => {
    if (!msg) return;
    msg.textContent = text;
    msg.hidden = false;
    msg.classList.toggle("ok", !!ok);
    msg.classList.toggle("err", !ok);
  };
  if (btn) { btn.disabled = true; btn.textContent = "Saving…"; }
  try {
    const res = await fetch("/api/account/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ models: Array.from(dashSelected) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't save your models.");
    currentAccount = data.customer;
    dashSeededFor = currentAccount.id; // dashSelected already equals the saved set
    const n = (data.customer.models || []).length;
    const stat = body.querySelector("#dashModelsStat");
    if (stat) stat.textContent = String(n);
    show(`Saved — your key now unlocks ${n} model${n === 1 ? "" : "s"}.`, true);
  } catch (e) {
    show(e.message, false);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Save my models"; }
  }
}

// DASHBOARD — top up via a REAL payment order (Phase 9). Creates the order on
// the server (/api/pay/create), opens the provider cashier (pay_url) or shows
// the WeChat QR string, then polls the order until the provider's VERIFIED
// notify lands server-side and the balance refreshes. No client-trusted money:
// the server credits only from the signed notify, never from this page.
async function doTopup(amount) {
  const body = $("detailBody");
  if (!body) return;
  if (amount == null) {
    const el = body.querySelector("#topupAmt");
    amount = el ? Number(el.value) : NaN;
  }
  const fail = (text) => {
    const msg = body.querySelector("#topupMsg");
    if (msg) { msg.textContent = text; msg.hidden = false; msg.classList.add("err"); msg.classList.remove("ok"); }
  };
  if (!amount || isNaN(amount) || amount <= 0) return fail("Enter an amount greater than zero.");
  try {
    const res = await fetch("/api/pay/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "topup", amount }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "下单失败，请稍后再试。");
    openPayPanel(data);
  } catch (e) {
    fail(e.message);
  }
}

// Show one created order's cashier panel and start polling it.
let payPollTimer = null;

function openPayPanel(order) {
  const body = $("detailBody");
  const panel = body && body.querySelector("#payPanel");
  if (!panel) return;
  const link = order.pay_url
    ? `<a href="${esc(order.pay_url)}" target="_blank" rel="noopener" class="run-btn" style="display:inline-block;text-decoration:none;">打开收银台支付 ${cny(order.amount)}</a>`
    : "";
  const qr = order.qr_text
    ? `<p class="muted" style="word-break:break-all;">微信扫码串（用任意二维码工具生成后扫码）：<code>${esc(order.qr_text)}</code></p>`
    : "";
  panel.innerHTML = `
    <div><strong>订单 ${esc(order.order_no)}</strong> · ${cny(order.amount)} · ${esc(order.provider)}</div>
    <div style="margin:8px 0;">${link}${qr}</div>
    <p class="muted">等待支付确认…（支付完成后余额自动更新，可随时关闭收银台页）</p>`;
  panel.hidden = false;
  if (order.pay_url) window.open(order.pay_url, "_blank", "noopener");
  pollOrder(order.order_no);
}

function pollOrder(orderNo) {
  if (payPollTimer) clearInterval(payPollTimer);
  let ticks = 0;
  payPollTimer = setInterval(async () => {
    ticks += 1;
    if (ticks > 150) { clearInterval(payPollTimer); payPollTimer = null; return; } // ~5 min
    try {
      const res = await fetch(`/api/pay/status/${encodeURIComponent(orderNo)}`);
      const data = await res.json();
      if (res.ok && data.status === "paid") {
        clearInterval(payPollTimer);
        payPollTimer = null;
        await refreshAccount(); // re-renders the dashboard: balance, plans, orders
        const msg = $("detailBody") && $("detailBody").querySelector("#topupMsg");
        if (msg) {
          msg.textContent = "✅ 支付成功，已到账。";
          msg.hidden = false; msg.classList.add("ok"); msg.classList.remove("err");
        }
      }
    } catch (e) { /* transient — keep polling */ }
  }, 2000);
}

// --- Phase 9: the plans + orders panels (filled in async after render) -----
async function loadBillingPanels(body) {
  const planBox = body.querySelector("#planBox");
  const ordersBox = body.querySelector("#ordersBox");
  if (!planBox && !ordersBox) return;
  try {
    const [plansRes, ordersRes] = await Promise.all([fetch("/api/plans"), fetch("/api/orders")]);
    const plans = await plansRes.json();
    const orders = ordersRes.ok ? await ordersRes.json() : { orders: [] };
    if (planBox) renderPlanBox(planBox, plans);
    if (ordersBox) renderOrdersBox(ordersBox, orders.orders || []);
  } catch (e) {
    if (planBox) planBox.innerHTML = `<p class="muted">套餐信息加载失败，请刷新重试。</p>`;
  }
}

function renderPlanBox(box, data) {
  const cur = data.current || {};
  const curCode = cur.plan_code || "free";
  const sub = cur.subscription;
  const cards = (data.plans || []).map((p) => {
    const isCur = p.code === curCode;
    const price = p.monthly_price_yuan > 0 ? `¥${p.monthly_price_yuan}/月` : "免费";
    const buy = p.code === "free" ? "" :
      `<button type="button" class="topup-quick plan-buy" data-plan="${esc(p.code)}">${isCur ? "续费" : "订购"} ${price}</button>`;
    return `<div class="shelf-item ${isCur ? "on" : ""}" style="flex-direction:column;align-items:flex-start;gap:5px;">
      <span class="shelf-name">${esc(p.label)}${isCur ? " · 当前 ✓" : ""}</span>
      <span class="shelf-id">每月 ${p.monthly_report_quota} 份报告 · ${p.seats} 个席位 · ${price}</span>
      ${buy}
    </div>`;
  }).join("");
  const usage = (cur.reports_used_this_month != null && cur.monthly_report_quota)
    ? `<p class="muted">本月已用 ${cur.reports_used_this_month}/${cur.monthly_report_quota} 份报告。${cur.locked ? " ⚠️ 订阅已逾期，功能已锁定（仅可导出数据），续费后恢复。" : ""}</p>`
    : "";
  let subLine = "";
  if (sub && sub.status !== "canceled") {
    const end = (sub.current_period_end || "").slice(0, 10);
    subLine = sub.cancel_at_period_end
      ? `<p class="muted">订阅将于 ${esc(end)} 到期后不再续订。 <button type="button" class="dash-group-all" id="subResume">恢复续订</button></p>`
      : `<p class="muted">当前订阅有效期至 ${esc(end)}（到期前 3 天会发送续费提醒，不自动扣款）。 <button type="button" class="dash-group-all" id="subCancel">到期后取消</button></p>`;
  }
  box.innerHTML = `<div class="shelf-grid">${cards}</div>${usage}${subLine}<p id="planMsg" class="dash-save-msg" hidden></p>`;
  box.querySelectorAll(".plan-buy").forEach((b) =>
    b.addEventListener("click", () => subscribePlan(b.dataset.plan)));
  const c = box.querySelector("#subCancel");
  if (c) c.addEventListener("click", () => toggleSubRenew("cancel"));
  const r = box.querySelector("#subResume");
  if (r) r.addEventListener("click", () => toggleSubRenew("resume"));
}

async function subscribePlan(planCode) {
  try {
    const res = await fetch("/api/pay/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "subscription", plan_code: planCode }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "下单失败");
    openPayPanel(data);
  } catch (e) {
    const msg = $("detailBody") && $("detailBody").querySelector("#planMsg");
    if (msg) { msg.textContent = e.message; msg.hidden = false; msg.classList.add("err"); }
  }
}

async function toggleSubRenew(which) {
  try {
    const res = await fetch(`/api/subscription/${which}`, { method: "POST" });
    if (!res.ok) throw new Error((await res.json()).error || "操作失败");
    const body = $("detailBody");
    if (body) loadBillingPanels(body);
  } catch (e) {
    const msg = $("detailBody") && $("detailBody").querySelector("#planMsg");
    if (msg) { msg.textContent = e.message; msg.hidden = false; msg.classList.add("err"); }
  }
}

function renderOrdersBox(box, orders) {
  if (!orders.length) {
    box.innerHTML = `<p class="muted">还没有订单 — 充值或订购套餐后会出现在这里。</p>`;
    return;
  }
  const stateCn = { created: "待支付", paid: "已支付", failed: "失败", refunded: "已退款" };
  const fapiaoCell = (o) => {
    if (o.status !== "paid") return "—";
    if (o.fapiao_status === "requested") return "审核中";
    if (o.fapiao_status === "issued") return "已开具";
    return `<button type="button" class="dash-group-all fapiao-req" data-order="${esc(o.order_no)}">申请发票</button>`;
  };
  const rows = orders.map((o) => `<tr>
      <td>${fmtTime(o.created_at)}</td>
      <td>${o.type === "subscription" ? `订阅 ${esc(o.plan_code || "")}` : "充值"}</td>
      <td>${cny(o.amount_yuan)}</td>
      <td>${stateCn[o.status] || esc(o.status)}${o.refunded_yuan ? `（已退 ${cny(o.refunded_yuan)}）` : ""}</td>
      <td>${esc(o.provider)}</td>
      <td>${fapiaoCell(o)}</td>
    </tr>`).join("");
  box.innerHTML = `<table class="dash-usage">
    <thead><tr><th>时间</th><th>类型</th><th>金额</th><th>状态</th><th>渠道</th><th>发票</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p id="fapiaoMsg" class="dash-save-msg" hidden></p>`;
  box.querySelectorAll(".fapiao-req").forEach((b) =>
    b.addEventListener("click", () => requestFapiao(b.dataset.order)));
}

// 申请发票 — collects 抬头 + 税号 (+ optional email), POSTs the request, then
// reloads the orders panel so the row flips to 审核中.
async function requestFapiao(orderNo) {
  const title = (window.prompt("发票抬头（公司名称）：") || "").trim();
  if (!title) return;
  const taxNo = (window.prompt("税号（统一社会信用代码）：") || "").trim();
  if (!taxNo) return;
  const email = (window.prompt("接收发票的邮箱（留空使用账户邮箱）：") || "").trim();
  const show = (text, ok) => {
    const msg = $("detailBody") && $("detailBody").querySelector("#fapiaoMsg");
    if (msg) {
      msg.textContent = text; msg.hidden = false;
      msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
    }
  };
  try {
    const res = await fetch("/api/fapiao/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ order_no: orderNo, title, tax_no: taxNo, email: email || undefined }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "申请失败");
    const body = $("detailBody");
    if (body) await loadBillingPanels(body);
    show("发票申请已提交，开具后将发送到邮箱。", true);
  } catch (e) {
    show(e.message, false);
  }
}

// ===========================================================================
// ACCOUNTS — email + password. Login lives in the top-right widget; sign-up is
// its own full-screen page. The dashboard reflects whoever is signed into THIS
// browser session (a signed cookie that holds only the customer id, server-side).
// ===========================================================================

// On boot: ask the server who (if anyone) is signed into this session.
async function loadMe() {
  try {
    const res = await fetch("/api/me");
    const data = await res.json();
    currentAccount = (res.ok && data && data.customer) ? data.customer : null;
  } catch (e) {
    currentAccount = null;
  }
  dashSeededFor = null; // re-seed the dashboard model editor for whoever is signed in
  brainSeededFor = null; // and re-seed the Company Brain page for this account
  renderAccountWidget();
}

// Re-pull the signed-in account from the server (the dashboard's "Refresh").
async function refreshAccount() {
  accountError = "";
  try {
    const res = await fetch("/api/me");
    const data = await res.json();
    if (res.ok && data && data.customer) {
      currentAccount = data.customer;
    } else {
      currentAccount = null;
      accountError = "Your session has ended — please log in again.";
    }
  } catch (e) {
    accountError = "Couldn't reach the server.";
  }
  renderAccountWidget();
  forceRefreshDetail();
}

// The top-right widget. Signed out → "Log in" + "Sign up". Signed in → the
// account name/email + "Dashboard" + "Log out". Buttons are wired on each render.
function renderAccountWidget() {
  const el = $("account");
  if (!el) return;
  if (currentAccount) {
    const c = currentAccount;
    el.innerHTML = `
      <span class="account-name">
        <span class="an-name">${esc(c.name)}</span>
        ${c.email ? `<span class="an-email">${esc(c.email)}</span>` : ""}
      </span>
      <button type="button" class="account-btn ghost" id="acctDashboard">Dashboard</button>
      <button type="button" class="account-btn" id="acctLogout">Log out</button>`;
    const dash = $("acctDashboard");
    if (dash) dash.addEventListener("click", () => openDetail("unified"));
    const out = $("acctLogout");
    if (out) out.addEventListener("click", doLogout);
  } else {
    el.innerHTML = `
      <button type="button" class="account-btn ghost" id="acctLogin">Log in</button>
      <button type="button" class="account-btn primary" id="acctSignup">Sign up</button>`;
    const login = $("acctLogin");
    if (login) login.addEventListener("click", openLoginModal);
    const signup = $("acctSignup");
    if (signup) signup.addEventListener("click", openSignup);
  }
  updateBrainNav(); // the 企业大脑 menu item + tab are logged-in only
}

// Show/hide the 企业大脑 (Company Brain) menu rectangle + detail tab based on
// whether anyone is signed into this browser. It's a logged-in-only workspace.
function updateBrainNav() {
  const show = !!currentAccount;
  const rect = $("menuRectBrain");
  const tab = $("detailTabBrain");
  if (rect) rect.hidden = !show;
  if (tab) tab.hidden = !show;
  const ob = $("menuRectOnboard");      // the wizard entry is logged-in only too
  if (ob) ob.hidden = !show;
}

// Wire the persistent account chrome: the login modal + the sign-up close button.
// (Escape-to-close is handled centrally in wireDrawer's keydown listener.)
function wireAccount() {
  const close = $("loginClose");
  if (close) close.addEventListener("click", closeLoginModal);
  const backdrop = $("loginBackdrop");
  if (backdrop) backdrop.addEventListener("click", closeLoginModal);
  const form = $("loginForm");
  if (form) form.addEventListener("submit", (e) => {
    e.preventDefault();
    doLogin($("loginEmail").value, $("loginPassword").value);
  });
  const toSignup = $("loginToSignup");
  if (toSignup) toSignup.addEventListener("click", () => { closeLoginModal(); openSignup(); });
  const toReset = $("loginToReset");
  if (toReset) toReset.addEventListener("click", () => {
    const email = ($("loginEmail").value || "").trim();
    closeLoginModal();
    openResetModal(email);
  });
  const signupClose = $("signupClose");
  if (signupClose) signupClose.addEventListener("click", closeSignup);
  wireResetModal();
}

// ---- Login modal ----------------------------------------------------------
function openLoginModal() {
  const m = $("loginModal");
  if (!m) return;
  hideLoginError();
  const form = $("loginForm");
  if (form) form.reset();
  m.hidden = false;
  requestAnimationFrame(() => m.classList.add("open"));
  m.setAttribute("aria-hidden", "false");
  const email = $("loginEmail");
  if (email) setTimeout(() => email.focus(), 60);
}

function closeLoginModal() {
  const m = $("loginModal");
  if (!m) return;
  m.classList.remove("open");
  m.setAttribute("aria-hidden", "true");
  setTimeout(() => { if (!m.classList.contains("open")) m.hidden = true; }, 200);
}

function showLoginError(msg) {
  const el = $("loginError");
  if (el) { el.textContent = msg; el.hidden = false; }
}
function hideLoginError() {
  const el = $("loginError");
  if (el) { el.hidden = true; el.textContent = ""; }
}

async function doLogin(email, password) {
  email = (email || "").trim();
  hideLoginError();
  if (!email || !password) { showLoginError("Enter your email and password."); return; }
  const btn = $("loginSubmit");
  if (btn) { btn.disabled = true; btn.textContent = "Logging in…"; }
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Email or password is incorrect.");
    currentAccount = data.customer;
    dashSeededFor = null; // let the dashboard editor seed fresh for this account
    brainSeededFor = null; // and the Company Brain page
    accountError = "";
    renderAccountWidget();
    closeLoginModal();
    // If they're on the Get Unified API page, swap the storefront for the dashboard.
    if (detailOpen && currentView() === "unified") forceRefreshDetail();
    // Deep-linked to /company-brain before logging in? Now they're in — open it.
    if (location.pathname === "/company-brain") openDetail("brain");
    loadChangelogState();          // the 新功能 badge is per-account
    // The ?onboarding=demo deep link, now that a session exists; otherwise the
    // standard first-login auto-open (until the wizard is done or dismissed).
    if (new URLSearchParams(location.search).get("onboarding") === "demo") {
      openOnboarding({ demo: true });
    } else {
      maybeAutoOpenOnboarding();
    }
  } catch (e) {
    showLoginError(e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Log in"; }
  }
}

async function doLogout() {
  try { await fetch("/api/logout", { method: "POST" }); }
  catch (e) { /* even if the call fails, drop local state */ }
  currentAccount = null;
  dashSeededFor = null;          // forget the previous account's model-editor seed
  dashSelected = new Set();
  accountError = "";
  // Drop the Company Brain cache and leave its page if we're on it (it's gated).
  brainSeededFor = null;
  brainProfile = null; brainGlossary = []; brainPrefs = []; brainKpiDraft = [];
  brainBranding = null; brainJobs = []; brainJobDraft = null; brainReports = [];
  if (brainImportTimer) { clearInterval(brainImportTimer); brainImportTimer = null; }
  brainImport = null;
  renderAccountWidget();
  if (detailOpen && currentView() === "brain") { closeDetail(); return; }
  if (detailOpen && currentView() === "unified") forceRefreshDetail();
}

// ---- Phone / SMS verification (shared by sign-up + password reset) ---------
// Whether the server requires phone + SMS-code verification (the China launch
// norm). Surfaced via /api/meta; default ON until we know otherwise.
function phoneVerifyOn() {
  return !META || META.phone_verification !== false;
}

// Request an SMS code from /api/send-code and run a short cooldown on the button
// so a customer can't hammer it (the server also caps 3 sends/phone/hour).
// `opts`: { phone, email, purpose, btn, status } — status is an element to show
// the result line in. Returns nothing; updates the UI as it goes.
async function requestSmsCode({ phone, email, purpose, btn, status }) {
  const setStatus = (msg, ok) => {
    if (!status) return;
    status.textContent = msg || "";
    status.style.color = ok ? "var(--accent-ink)" : "var(--red)";
  };
  phone = (phone || "").trim();
  email = (email || "").trim();
  if (!phone && !(purpose === "reset" && email)) {
    setStatus("请输入手机号。", false);
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = "发送中…"; }
  try {
    const res = await fetch("/api/send-code", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone, email, purpose }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "发送失败，请稍后再试。");
    setStatus("验证码已发送，请查收短信（5 分钟内有效）。", true);
    startSendCooldown(btn, 60);
  } catch (e) {
    setStatus(e.message, false);
    if (btn) { btn.disabled = false; btn.textContent = "发送验证码"; }
  }
}

// Disable a "send code" button for `secs` seconds, counting down in its label.
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

// ---- Reset-password modal --------------------------------------------------
function openResetModal(prefillEmail) {
  const m = $("resetModal");
  if (!m) return;
  const form = $("resetForm");
  if (form) form.reset();
  hideEl("resetError"); hideEl("resetMsg");
  if (prefillEmail) { const e = $("resetEmail"); if (e) e.value = prefillEmail; }
  m.hidden = false;
  requestAnimationFrame(() => m.classList.add("open"));
  m.setAttribute("aria-hidden", "false");
  const phone = $("resetPhone");
  if (phone) setTimeout(() => phone.focus(), 60);
}

function closeResetModal() {
  const m = $("resetModal");
  if (!m) return;
  m.classList.remove("open");
  m.setAttribute("aria-hidden", "true");
  setTimeout(() => { if (!m.classList.contains("open")) m.hidden = true; }, 200);
}

// Small show/hide helpers for the reset modal's status lines.
function showEl(id, msg) { const el = $(id); if (el) { el.textContent = msg; el.hidden = false; } }
function hideEl(id) { const el = $(id); if (el) { el.hidden = true; el.textContent = ""; } }

function wireResetModal() {
  const close = $("resetClose");
  if (close) close.addEventListener("click", closeResetModal);
  const backdrop = $("resetBackdrop");
  if (backdrop) backdrop.addEventListener("click", closeResetModal);
  const toLogin = $("resetToLogin");
  if (toLogin) toLogin.addEventListener("click", () => { closeResetModal(); openLoginModal(); });
  const send = $("resetSendCode");
  if (send) send.addEventListener("click", () => {
    hideEl("resetError");
    requestSmsCode({
      phone: ($("resetPhone") || {}).value,
      email: ($("resetEmail") || {}).value,
      purpose: "reset",
      btn: send,
      status: $("resetMsg"),
    });
    // requestSmsCode writes into #resetMsg; reveal it.
    const m = $("resetMsg"); if (m) m.hidden = false;
  });
  const form = $("resetForm");
  if (form) form.addEventListener("submit", (e) => { e.preventDefault(); doResetPassword(); });
}

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
  if (btn) { btn.disabled = true; btn.textContent = "重置中…"; }
  try {
    const res = await fetch("/api/reset-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone, email, code, new_password: newPassword }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "重置失败，请确认信息后重试。");
    hideEl("resetMsg");
    showEl("resetMsg", "密码已重置，请用新密码登录。");
    setTimeout(() => { closeResetModal(); openLoginModal(); }, 1200);
  } catch (e) {
    showEl("resetError", e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Reset password"; }
  }
}

// ---- Sign-up page (a brand-new full-screen account-creation flow) ----------
function openSignup() {
  // Always start clean (the page is shown once; a fresh key isn't remembered).
  signupKey = null;
  signupCustomer = null;
  signupError = "";
  const v = $("signupView");
  if (!v) return;
  renderSignup();
  v.classList.add("open");
  v.setAttribute("aria-hidden", "false");
}

function closeSignup() {
  const v = $("signupView");
  if (!v) return;
  v.classList.remove("open");
  v.setAttribute("aria-hidden", "true");
  // Forget a freshly-issued key the moment the page closes (shown once).
  signupKey = null;
  signupCustomer = null;
  signupError = "";
  // A brand-new account just saved its key — start the onboarding wizard.
  maybeAutoOpenOnboarding();
}

function renderSignup() {
  const body = $("signupBody");
  if (!body) return;
  body.innerHTML = signupBodyHTML();
  wireSignupBody();
  body.scrollTop = 0;
}

function signupBodyHTML() {
  // After a successful sign-up we replace the form with a one-time key reveal.
  if (signupKey && signupCustomer) {
    return `<div class="signup-card">
      <h1 class="signup-title">You're in, ${esc(signupCustomer.name)} 🎉</h1>
      <p class="signup-lead">Your account is created and you're now logged in. Copy your API key below — for your security it won't be shown again.</p>
      ${keyRevealHTML(signupKey, signupCustomer)}
      <button type="button" id="signupDone" class="run-btn" style="margin-top:22px;">Go to my dashboard</button>
    </div>`;
  }

  const err = signupError
    ? `<p class="form-error" style="display:block">${esc(signupError)}</p>` : "";

  // Phone + SMS code are required by default (the China launch norm). The server
  // exposes the flag via /api/meta so we only ask when verification is actually on.
  const phoneBlock = phoneVerifyOn() ? `
        <div class="field">
          <label class="field-label" for="suPhone">手机号 Phone</label>
          <input class="field-input" type="tel" id="suPhone" placeholder="13800000000" autocomplete="tel" />
        </div>
        <div class="field">
          <label class="field-label" for="suCode">验证码 SMS code</label>
          <div class="code-row">
            <input class="field-input code-input" type="text" id="suCode" inputmode="numeric" maxlength="6" placeholder="6-digit code" autocomplete="one-time-code" />
            <button type="button" class="code-btn" id="suSendCode">发送验证码</button>
          </div>
          <p id="suCodeMsg" class="modal-hint" style="margin-top:6px;"></p>
        </div>` : "";

  return `<div class="signup-card">
    <h1 class="signup-title">Create your account</h1>
    <p class="signup-lead">One account, one key, every model. Tell us who you are and we'll mint your unified API key — then you pick the models it unlocks from your dashboard.</p>
    <form id="signupForm" autocomplete="on">
      <div class="signup-grid">
        <div class="field">
          <label class="field-label" for="suName">Name</label>
          <input class="field-input" type="text" id="suName" placeholder="Ada Lovelace" autocomplete="name" />
        </div>
        <div class="field">
          <label class="field-label" for="suCompany">Company <span class="field-optional">(optional)</span></label>
          <input class="field-input" type="text" id="suCompany" placeholder="Analytical Engines Ltd." autocomplete="organization" />
        </div>
        <div class="field">
          <label class="field-label" for="suEmail">Email</label>
          <input class="field-input" type="email" id="suEmail" placeholder="you@company.com" autocomplete="email" />
        </div>
        <div class="field">
          <label class="field-label" for="suPassword">Password <span class="field-optional">(8+ characters)</span></label>
          <input class="field-input" type="password" id="suPassword" placeholder="Choose a password" autocomplete="new-password" />
        </div>
        ${phoneBlock}
      </div>

      <p class="signup-note">After you sign up you'll land on your dashboard, where you can tick the models your key should unlock — across all ${Object.keys(PROVIDERS).length || 11} providers — and change them anytime.</p>

      ${err}
      <button type="submit" class="run-btn signup-submit" id="signupSubmit">Create my account &amp; key</button>
    </form>
  </div>`;
}

function wireSignupBody() {
  const body = $("signupBody");
  if (!body) return;

  // The one-time reveal's "Go to my dashboard" + its copy button.
  const done = body.querySelector("#signupDone");
  if (done) {
    const theKey = signupKey; // capture before closeSignup() forgets it
    done.addEventListener("click", () => { closeSignup(); openDetail("unified"); });
    const krCopy = body.querySelector("#krCopy");
    if (krCopy) krCopy.addEventListener("click", () => {
      navigator.clipboard.writeText(theKey || "").then(() => {
        krCopy.textContent = "Copied!";
        setTimeout(() => (krCopy.textContent = "Copy"), 1500);
      });
    });
    return;
  }

  // Sign-up collects account fields only — model selection happens on the dashboard
  // after login (it's gated behind the account), so there's no picker to wire here.
  const form = body.querySelector("#signupForm");
  if (form) form.addEventListener("submit", (e) => { e.preventDefault(); doSignup(); });

  // The "send code" button (only present when phone verification is on).
  const send = body.querySelector("#suSendCode");
  if (send) send.addEventListener("click", () => {
    requestSmsCode({
      phone: (body.querySelector("#suPhone") || {}).value,
      purpose: "signup",
      btn: send,
      status: body.querySelector("#suCodeMsg"),
    });
  });
}

async function doSignup() {
  const body = $("signupBody");
  if (!body) return;
  const name = (body.querySelector("#suName").value || "").trim();
  const company = (body.querySelector("#suCompany").value || "").trim();
  const email = (body.querySelector("#suEmail").value || "").trim();
  const password = body.querySelector("#suPassword").value || "";
  const phone = ((body.querySelector("#suPhone") || {}).value || "").trim();
  const code = ((body.querySelector("#suCode") || {}).value || "").trim();

  // Client-side checks (the server re-validates all of this). Model selection is
  // NOT part of sign-up anymore — accounts start empty and pick models on the
  // dashboard, so there's nothing to validate here beyond the account fields.
  if (!name) return failSignup("Please tell us your name.");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return failSignup("Enter a valid email address.");
  if (password.length < 8) return failSignup("Use a password of at least 8 characters.");
  if (phoneVerifyOn()) {
    if (!phone) return failSignup("请输入手机号。");
    if (!code) return failSignup("请输入收到的短信验证码。");
  }

  const btn = body.querySelector("#signupSubmit");
  if (btn) { btn.disabled = true; btn.textContent = "Creating…"; }
  try {
    const res = await fetch("/api/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, company, email, password, phone, code }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not create your account.");
    // Success: the server set the session, so we're logged in. Reveal key ONCE.
    signupKey = data.api_key;
    signupCustomer = data.customer;
    currentAccount = data.customer;
    dashSeededFor = null; // let the dashboard editor seed fresh for this new account
    signupError = "";
    renderAccountWidget();
    renderSignup(); // swap the form for the one-time key reveal
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "Create my account & key"; }
    failSignup(e.message, { name, company, email, password, phone, code });
  }
}

// Show a sign-up error. We re-render the form (the error line lives in its HTML),
// then restore any values the customer had typed — crucially the SMS code, which
// is time-limited and rate-capped, so losing it on a validation slip is painful.
function failSignup(msg, keep) {
  signupError = msg;
  renderSignup();
  if (!keep) return;
  const body = $("signupBody");
  if (!body) return;
  const set = (id, v) => { const el = body.querySelector(id); if (el && v != null) el.value = v; };
  set("#suName", keep.name); set("#suCompany", keep.company);
  set("#suEmail", keep.email); set("#suPassword", keep.password);
  set("#suPhone", keep.phone); set("#suCode", keep.code);
}

// The Token Manager, merged into the Team detail view (no own page header).
function tokensSectionHTML() {
  const s = lastTokens;
  if (!s) {
    return `<div class="detail-section">
      <div class="block-title">Token Manager · CFO</div>
      <p class="muted">Loading budget data…</p>
    </div>`;
  }
  const t = s.total;
  const overall = (t.unlimited || t.limit == null)
    ? `<div class="tk-overall"><span class="tk-big">${cny(t.used)}</span><span class="tk-of">spent · no limit</span></div>
       <div class="meter"><div class="meter-fill ok" style="width:0%"></div></div>`
    : `<div class="tk-overall"><span class="tk-big">${cny(t.used)}</span><span class="tk-of">of ${cny(t.limit)} (${t.pct_used}%)</span></div>
       <div class="meter"><div class="meter-fill ${esc(t.state)}" style="width:${Math.min(t.pct_used, 100)}%"></div></div>`;
  const warns = (s.warnings && s.warnings.length)
    ? `<div class="token-warnings" style="margin-top:18px;">${s.warnings.map((w) => `<div>${esc(w)}</div>`).join("")}</div>`
    : "";
  const budgets = s.budgets.length
    ? `<div class="tk-grid">${s.budgets.map(budgetCardHTML).join("")}</div>`
    : `<p class="muted">No task budgets yet. Add one below.</p>`;
  return `<div class="detail-section">
      <div class="block-title">Token Manager · CFO — overall budget</div>
      <p class="section-hint">Tracks &amp; limits the whole team's spend, in ¥ (converted from each model's real price).</p>
      ${overall}
      ${warns}
      <form id="dTotalForm" class="inline-form" style="margin-top:14px;max-width:430px;">
        <input type="number" id="dTotalInput" min="0" step="50" placeholder="set total ¥ (0 = no limit)" />
        <button type="submit">Set</button>
      </form>
    </div>
    <div class="detail-section">
      <div class="block-title">Task budgets</div>
      ${budgets}
      <form id="dBudgetForm" class="inline-form stacked" style="max-width:430px;margin-top:14px;">
        <input type="text" id="dBudgetName" placeholder="task name (e.g. video_generation)" />
        <div class="inline-form">
          <input type="number" id="dBudgetLimit" min="0" step="50" placeholder="¥ limit" />
          <button type="submit">Add / update</button>
        </div>
      </form>
      <button id="dResetTokens" class="ghost-btn" style="max-width:430px;margin-top:12px;">Reset spend counters</button>
    </div>`;
}

function budgetCardHTML(b) {
  const unlimited = b.unlimited || b.limit == null;
  const limitTxt = unlimited ? "no limit" : cny(b.limit);
  const pct = unlimited ? 0 : Math.min(b.pct_used, 100);
  const cls = unlimited ? "ok" : b.state;
  const badgeTxt = unlimited ? "tracking"
    : b.state === "ok" ? "ok" : b.state === "warning" ? "low" : "cut off";
  return `<div class="budget-item">
    <div class="budget-top">
      <span class="budget-name">${esc(b.name)}</span>
      <span style="display:flex;align-items:center;gap:8px;">
        <span class="budget-badge badge-${esc(cls)}">${badgeTxt}</span>
        <button class="budget-rm" title="Remove" data-name="${esc(b.name)}">&times;</button>
      </span>
    </div>
    <div class="budget-vals">${cny(b.used)} / ${limitTxt} spent</div>
    <div class="meter"><div class="meter-fill ${esc(cls)}" style="width:${pct}%"></div></div>
  </div>`;
}

// Friendly model label from the raw model id (e.g. "claude-opus-4-7" → "Opus").
function modelLabel(m) {
  const s = String(m || "").toLowerCase();
  if (s.includes("opus")) return "Opus";
  if (s.includes("sonnet")) return "Sonnet";
  if (s.includes("haiku")) return "Haiku";
  return m || "—";
}

// ===========================================================================
// COMPANY BRAIN (企业大脑) — the team's persistent long-term memory.
// Four editable sections (公司档案 / 指标定义 / 术语表 / 报告偏好) + a historical
// import. Everything is keyed on the signed-in account's company row server-side;
// this page is the UI plus the "agent asks once, remembers forever" learning loop.
// Logged-in only (the menu item + tab are hidden when signed out).
// ===========================================================================
const BRAIN_INDUSTRIES = ["跨境电商", "贸易", "制造", "服务", "其他"];
const BRAIN_IMPORT_MAX = 12;

function brainDetailHTML() {
  if (!currentAccount) {
    return `<h2 class="detail-h">企业大脑</h2>
      <p class="detail-lead">请先登录后再使用企业大脑。</p>`;
  }
  // Load this account's brain once; show a light shell while it arrives.
  if (brainSeededFor !== currentAccount.id) {
    if (!brainLoading) loadBrain();
    return `<h2 class="detail-h">企业大脑</h2>
      <p class="detail-lead">正在载入公司记忆…</p>`;
  }

  const err = brainError
    ? `<p class="form-error" style="display:block">${esc(brainError)}</p>` : "";
  const msg = brainMsg
    ? `<p class="dash-save-msg ok" style="display:block">${esc(brainMsg)}</p>` : "";

  return `
    <h2 class="detail-h">企业大脑</h2>
    <p class="detail-lead">这里是 AI 团队的长期记忆。维护好公司档案、指标定义、术语表与报告偏好，
      每次分析都会自动读取，结果越来越懂你的业务。</p>
    ${err}${msg}
    ${brainProfileSectionHTML()}
    ${brainBrandingSectionHTML()}
    ${brainKpiSectionHTML()}
    ${brainGlossarySectionHTML()}
    ${brainPrefsSectionHTML()}
    ${brainScheduleSectionHTML()}
    ${brainReportsSectionHTML()}
    ${brainImportSectionHTML()}`;
}

// a. 公司档案 — name / industry / free-text description / fiscal week start.
function brainProfileSectionHTML() {
  const p = brainProfile || {};
  const industry = p.industry || "";
  const fw = p.fiscal_week_start === "Sun" ? "Sun" : "Mon";
  const opts = BRAIN_INDUSTRIES.map((i) =>
    `<option value="${esc(i)}" ${i === industry ? "selected" : ""}>${esc(i)}</option>`).join("");
  // Keep an industry that isn't in the standard list (from an earlier save).
  const extra = (industry && !BRAIN_INDUSTRIES.includes(industry))
    ? `<option value="${esc(industry)}" selected>${esc(industry)}</option>` : "";
  return `
    <div class="detail-section">
      <div class="block-title">公司档案</div>
      <p class="section-hint">团队在分析前会先读取这些信息，用来理解你的业务背景。</p>
      <div class="brain-grid">
        <label class="field">
          <span class="field-label">公司名称</span>
          <input class="field-input" id="brainName" type="text" value="${esc(p.company_name || "")}" placeholder="例如：星河跨境贸易" />
        </label>
        <label class="field">
          <span class="field-label">行业</span>
          <select class="field-input" id="brainIndustry">
            <option value="">（未选择）</option>
            ${opts}${extra}
          </select>
        </label>
      </div>
      <label class="field" style="margin-top:12px;display:block;">
        <span class="field-label">业务说明</span>
        <textarea class="field-input" id="brainDesc" rows="3" placeholder="一句话描述你的业务、主要产品或市场…">${esc(p.business_desc || "")}</textarea>
      </label>
      <div class="field" style="margin-top:12px;">
        <span class="field-label">财务周起始日</span>
        <div class="brain-radio-row">
          <label class="brain-radio"><input type="radio" name="brainFiscal" value="Mon" ${fw === "Mon" ? "checked" : ""}/> 周一 (Mon)</label>
          <label class="brain-radio"><input type="radio" name="brainFiscal" value="Sun" ${fw === "Sun" ? "checked" : ""}/> 周日 (Sun)</label>
        </div>
      </div>
      <button type="button" id="brainSaveProfile" class="run-btn" style="margin-top:16px;max-width:240px;">保存公司档案</button>
    </div>`;
}

// b. 指标定义 (KPI mapping) — each row maps a KPI to its column aliases + unit.
function brainKpiSectionHTML() {
  const rows = brainKpiDraft.map((m, i) => `
    <div class="kpi-row" data-i="${i}">
      <input class="field-input kpi-name" type="text" value="${esc(m.kpi_name_cn || "")}" placeholder="指标名（中文）如 毛利" />
      <input class="field-input kpi-aliases" type="text" value="${esc(m.aliases || "")}" placeholder="列名/别名，用逗号分隔：毛利,毛利润,gross profit" />
      <input class="field-input kpi-unit" type="text" value="${esc(m.unit || "")}" placeholder="单位 如 CNY" />
      <button type="button" class="ghost-btn kpi-rm" data-i="${i}" title="删除这一行">删除</button>
    </div>`).join("");
  const empty = brainKpiDraft.length ? ""
    : `<p class="muted">还没有指标定义。点「添加一行」告诉团队该如何识别你的关键指标。</p>`;
  return `
    <div class="detail-section">
      <div class="block-title">指标定义 (KPI 映射)</div>
      <p class="section-hint">把表格里五花八门的列名映射到统一的指标。分析前，团队会先用这些别名把列对上号；
        对不上的会直接说明，而不会乱猜。</p>
      <div class="kpi-table">${rows}</div>
      ${empty}
      <div class="brain-row-actions">
        <button type="button" id="brainAddKpi" class="ghost-btn">+ 添加一行</button>
        <button type="button" id="brainSaveKpi" class="run-btn" style="max-width:200px;">保存指标定义</button>
      </div>
    </div>`;
}

// c. 术语表 — term → definition rows (each row is editable + deletable).
function brainGlossarySectionHTML() {
  const rows = brainGlossary.map((g) => `
    <div class="gloss-row" data-id="${g.id}">
      <input class="field-input gloss-term" type="text" value="${esc(g.term)}" placeholder="术语" />
      <input class="field-input gloss-def" type="text" value="${esc(g.definition || "")}" placeholder="定义/含义" />
      <button type="button" class="ghost-btn gloss-save" data-id="${g.id}">保存</button>
      <button type="button" class="ghost-btn gloss-del" data-id="${g.id}">删除</button>
    </div>`).join("");
  const empty = brainGlossary.length ? ""
    : `<p class="muted">术语表还是空的。把团队可能不懂的缩写、渠道名、内部叫法加进来。</p>`;
  return `
    <div class="detail-section">
      <div class="block-title">术语表</div>
      <p class="section-hint">团队遇到不懂的名词，会在结果页问你一次；你在这里维护的定义，之后每次分析都会自动带上。</p>
      <div class="gloss-table">${rows}</div>
      ${empty}
      <div class="gloss-add">
        <input class="field-input" id="brainGlossTerm" type="text" placeholder="新术语，如 KA渠道" />
        <input class="field-input" id="brainGlossDef" type="text" placeholder="定义，如 重点大客户渠道" />
        <button type="button" id="brainAddGloss" class="run-btn" style="max-width:140px;">添加</button>
      </div>
    </div>`;
}

// d. 报告偏好 — standing instructions with an on/off toggle + add box.
function brainPrefsSectionHTML() {
  const rows = brainPrefs.map((p) => `
    <div class="pref-row" data-id="${p.id}">
      <label class="brain-switch">
        <input type="checkbox" class="pref-toggle" data-id="${p.id}" ${p.active ? "checked" : ""} />
        <span>${p.active ? "启用" : "停用"}</span>
      </label>
      <span class="pref-text">${esc(p.instruction)}</span>
      <button type="button" class="ghost-btn pref-del" data-id="${p.id}">删除</button>
    </div>`).join("");
  const empty = brainPrefs.length ? ""
    : `<p class="muted">还没有报告偏好。加一条试试，例如「区域拆分放在报告最前面」。</p>`;
  return `
    <div class="detail-section">
      <div class="block-title">报告偏好</div>
      <p class="section-hint">这些是写报告时的固定要求；启用的偏好会在每次生成报告时自动生效。</p>
      <div class="pref-table">${rows}</div>
      ${empty}
      <div class="gloss-add">
        <input class="field-input" id="brainPrefInput" type="text" placeholder="新增偏好，如 区域拆分放在报告最前面" />
        <button type="button" id="brainAddPref" class="run-btn" style="max-width:140px;">添加</button>
      </div>
    </div>`;
}

// 品牌设置 — logo + brand colour + 企业微信 webhook (drives the branded PDF + delivery).
function brainBrandingSectionHTML() {
  const b = brainBranding || {};
  const color = b.brand_color || "#1D9E75";
  const webhook = b.wechat_webhook || "";
  const logoState = b.has_logo
    ? `<img class="brand-logo-preview" src="/api/company-brain/logo?t=${Date.now()}" alt="公司 Logo" />`
    : `<span class="muted">尚未上传 Logo。</span>`;
  return `
    <div class="detail-section">
      <div class="block-title">品牌设置</div>
      <p class="section-hint">上传公司 Logo、设置品牌主色与企业微信群机器人 webhook。生成的 PDF 周报封面会用到 Logo 与主色；
        webhook 用于把报告推送到企业微信群（你填的地址只保存在公司大脑里，绝不会写进日志）。</p>
      <div class="brain-grid">
        <label class="field">
          <span class="field-label">公司 Logo（PNG / JPG，≤ 1MB）</span>
          <input class="field-input" id="brandLogo" type="file" accept=".png,.jpg,.jpeg" />
        </label>
        <div class="field">
          <span class="field-label">当前 Logo</span>
          <div class="brand-logo-box">${logoState}</div>
        </div>
      </div>
      <div class="brain-grid" style="margin-top:12px;">
        <label class="field">
          <span class="field-label">品牌主色</span>
          <span class="brand-color-row">
            <input id="brandColor" type="color" value="${esc(color)}" />
            <input class="field-input" id="brandColorHex" type="text" value="${esc(color)}" placeholder="#1D9E75" />
          </span>
        </label>
        <label class="field">
          <span class="field-label">企业微信群机器人 Webhook（可选）</span>
          <input class="field-input" id="brandWebhook" type="text" value="${esc(webhook)}" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…" />
        </label>
      </div>
      <button type="button" id="brainSaveBranding" class="run-btn" style="margin-top:16px;max-width:240px;">保存品牌设置</button>
    </div>`;
}

// 定时任务 — preset frequency dropdown (or custom cron), goal, delivery channels, budget.
const FREQ_PRESETS = [
  { label: "每周一 08:00", cron: "0 8 * * 1" },
  { label: "每月 1 号 08:00", cron: "0 8 1 * *" },
  { label: "自定义 cron", cron: "" },
];

function defaultJobDraft() {
  return { name_cn: "", presetIdx: 0, cron: "", goal_template: "生成上周经营周报",
           channels: ["email"], budget_cny: null };
}

function channelLabel(c) {
  return { email: "邮件", wechat: "企业微信", service_account: "服务号" }[c] || c;
}

function jobFreqLabel(cron) {
  const m = FREQ_PRESETS.find((p) => p.cron && p.cron === cron);
  return m ? m.label : ("cron: " + cron);
}

function brainScheduleSectionHTML() {
  const d = brainJobDraft || defaultJobDraft();
  const presetOpts = FREQ_PRESETS.map((p, i) =>
    `<option value="${i}" ${d.presetIdx === i ? "selected" : ""}>${esc(p.label)}</option>`).join("");
  const isCustom = d.presetIdx === FREQ_PRESETS.length - 1;
  const customRow = isCustom
    ? `<label class="field" style="margin-top:12px;display:block;max-width:360px;">
         <span class="field-label">自定义 cron（5 段：分 时 日 月 周）</span>
         <input class="field-input" id="jobCron" type="text" value="${esc(d.cron || "")}" placeholder="如 0 8 * * 1" />
       </label>` : "";
  const rows = brainJobs.map((j) => {
    const chans = (j.delivery_channels || []).map(channelLabel).join("、") || "—";
    const last = j.last_run_at ? `${esc(j.last_status || "")} · ${esc(j.last_run_at)}` : "尚未运行";
    return `
      <div class="job-row" data-id="${j.id}">
        <div class="job-main">
          <div class="job-name">${esc(j.name_cn)}${j.active ? "" : ' <span class="muted">(已停用)</span>'}</div>
          <div class="muted">${esc(jobFreqLabel(j.cron_expr))} · 交付：${esc(chans)} · 上次：${last}</div>
        </div>
        <div class="job-actions">
          <button type="button" class="ghost-btn job-toggle" data-id="${j.id}" data-active="${j.active ? 1 : 0}">${j.active ? "停用" : "启用"}</button>
          <button type="button" class="ghost-btn job-del" data-id="${j.id}">删除</button>
        </div>
      </div>`;
  }).join("");
  const empty = brainJobs.length ? ""
    : `<p class="muted">还没有定时任务。在下面新建一个，让团队按时自动生成并交付周报。</p>`;
  return `
    <div class="detail-section">
      <div class="block-title">定时任务</div>
      <p class="section-hint">让 AI 团队按你的节奏自动跑起来：到点用你最近上传的数据生成品牌周报，并通过邮件 / 企业微信发出。
        每次运行都照常计入账户余额；余额不足时不会运行，并会发「余额不足」通知。</p>
      <div class="job-list">${rows}</div>
      ${empty}
      <div class="job-form">
        <div class="block-title" style="margin-top:18px;">新建定时任务</div>
        <div class="brain-grid">
          <label class="field">
            <span class="field-label">任务名称</span>
            <input class="field-input" id="jobName" type="text" value="${esc(d.name_cn || "")}" placeholder="例如：每周经营周报" />
          </label>
          <label class="field">
            <span class="field-label">频率</span>
            <select class="field-input" id="jobPreset">${presetOpts}</select>
          </label>
        </div>
        ${customRow}
        <label class="field" style="margin-top:12px;display:block;">
          <span class="field-label">目标描述</span>
          <textarea class="field-input" id="jobGoal" rows="2" placeholder="生成上周经营周报">${esc(d.goal_template || "")}</textarea>
        </label>
        <div class="field" style="margin-top:12px;">
          <span class="field-label">交付方式</span>
          <div class="brain-radio-row">
            <label class="brain-radio"><input type="checkbox" class="job-chan" value="email" ${(d.channels || []).includes("email") ? "checked" : ""}/> 邮件</label>
            <label class="brain-radio"><input type="checkbox" class="job-chan" value="wechat" ${(d.channels || []).includes("wechat") ? "checked" : ""}/> 企业微信群机器人</label>
          </div>
        </div>
        <label class="field" style="margin-top:12px;display:block;max-width:260px;">
          <span class="field-label">预算上限 ¥（可选）</span>
          <input class="field-input" id="jobBudget" type="number" min="0" step="0.01" value="${d.budget_cny != null ? esc(String(d.budget_cny)) : ""}" placeholder="如 5.00（留空＝账户默认）" />
        </label>
        <button type="button" id="brainAddJob" class="run-btn" style="margin-top:16px;max-width:240px;">创建定时任务</button>
      </div>
    </div>`;
}

// 历史报告 — every completed run's branded PDF, downloadable (this account only).
function runStatusLabel(s) {
  return { succeeded: "已完成", running: "进行中", failed: "失败", stopped_budget: "预算用尽" }[s] || s;
}

function brainReportsSectionHTML() {
  const rows = brainReports.map((r) => {
    const dl = r.has_pdf
      ? `<a class="ghost-btn report-dl" href="${esc(r.download_url || "")}" target="_blank" rel="noopener">下载 PDF</a>`
      : `<span class="muted">无 PDF</span>`;
    const when = esc(r.finished_at || r.started_at || "");
    const goal = esc((r.goal_text || "").split("\n")[0].slice(0, 48) || "（无目标）");
    return `
      <div class="report-row">
        <div class="report-main">
          <div class="report-goal">${goal}</div>
          <div class="muted">${when} · ${runStatusLabel(r.status)} · ${cny(r.total_cost_yuan || 0, 4)}</div>
        </div>
        <div class="report-actions">${dl}</div>
      </div>`;
  }).join("");
  const empty = brainReports.length ? ""
    : `<p class="muted">还没有历史报告。运行一次任务后，这里会列出可下载的品牌 PDF。</p>`;
  return `
    <div class="detail-section">
      <div class="block-title">历史报告</div>
      <p class="section-hint">每次完成的运行都会生成一份品牌 PDF，存放在这里随时下载（下载链接需登录，且仅本账户可见）。</p>
      <div class="report-list">${rows}</div>
      ${empty}
    </div>`;
}

// 导入历史数据 — upload up to BRAIN_IMPORT_MAX past spreadsheets; the team runs only
// the cheap metric-extraction path. Estimate the ¥ cost, then confirm + watch progress.
function brainImportSectionHTML() {
  const imp = brainImport;
  const head = `<div class="block-title">导入历史数据</div>
    <p class="section-hint">一次最多上传 ${BRAIN_IMPORT_MAX} 个历史表格，团队只做「指标提取」（用便宜的快速模型），
      把过往数字录入公司大脑，不生成完整报告。开始前会先估算花费。</p>`;

  if (!imp || !imp.open) {
    return `<div class="detail-section">${head}
      <button type="button" id="brainImportOpen" class="ghost-btn" style="max-width:200px;">导入历史数据…</button>
    </div>`;
  }

  const err = imp.error ? `<p class="form-error" style="display:block">${esc(imp.error)}</p>` : "";
  const fileCount = (imp.files || []).length;
  const picked = fileCount ? `<p class="muted">已选择 ${fileCount} 个文件。</p>` : "";

  let estimateBlock = "";
  if (imp.estimate) {
    const e = imp.estimate;
    const list = (e.files || []).map((f) =>
      `<li>${esc(f.name)} <span class="muted">· ${fmtNum(f.chars)} 字 · 约 ${cny(f.cost, 4)}</span></li>`).join("");
    estimateBlock = `
      <div class="brain-estimate">
        <p>预计花费约 <strong>${cny(e.estimated_cost, 4)}</strong>（共 ${e.file_count} 个文件），确认导入？</p>
        <ul class="brain-file-list">${list}</ul>
        <button type="button" id="brainImportConfirm" class="run-btn" style="max-width:200px;" ${imp.starting ? "disabled" : ""}>
          ${imp.starting ? "正在导入…" : "确认导入"}</button>
      </div>`;
  }

  const progress = imp.job ? `<div id="brainImportProgress">${importProgressHTML()}</div>` : "";

  return `<div class="detail-section">${head}
    ${err}
    <input id="brainImportFiles" type="file" multiple
      accept=".xlsx,.xls,.csv,.pdf,.txt,.docx,.json,.md" />
    ${picked}
    <div class="brain-row-actions">
      <button type="button" id="brainImportEstimate" class="ghost-btn" ${imp.estimating ? "disabled" : ""} style="max-width:200px;">
        ${imp.estimating ? "正在估算…" : "预估花费"}</button>
    </div>
    ${estimateBlock}
    ${progress}
  </div>`;
}

function importProgressHTML() {
  const job = brainImport && brainImport.job;
  if (!job) return "";
  const statusMap = {
    running: ["导入中…", "pill-running"],
    done: ["导入完成", "pill-done"],
    stopped_budget: ["余额用尽，已停止", "pill-error"],
  };
  const [label, cls] = statusMap[job.status] || ["…", "pill-running"];
  const fileStatus = { pending: "等待中", working: "处理中", done: "完成", error: "失败", stopped_budget: "已停止" };
  const rows = (job.files || []).map((f) => `
    <li class="brain-import-file">
      <span>${esc(f.name)}</span>
      <span class="muted">${fileStatus[f.status] || f.status}${f.status === "done" ? ` · ${f.metrics} 条指标` : ""}</span>
    </li>`).join("");
  return `
    <div class="brain-import-head">
      <span class="pill ${cls}">${label}</span>
      <span class="muted">${job.done}/${job.total} 个文件 · 已花费 ${cny(job.cost, 4)}</span>
    </div>
    <ul class="brain-file-list">${rows}</ul>`;
}

// Load the signed-in account's brain (profile + glossary + preferences) once.
async function loadBrain() {
  if (!currentAccount) return;
  brainLoading = true;
  brainError = "";
  try {
    const [pr, gl, pf, br, jb, rp] = await Promise.all([
      fetch("/api/company-brain/profile").then((r) => r.json()),
      fetch("/api/company-brain/glossary").then((r) => r.json()),
      fetch("/api/company-brain/preferences").then((r) => r.json()),
      fetch("/api/company-brain/branding").then((r) => r.json()),
      fetch("/api/scheduled-jobs").then((r) => r.json()),
      fetch("/api/reports").then((r) => r.json()),
    ]);
    brainProfile = (pr && pr.profile) || {};
    brainKpiDraft = Array.isArray(brainProfile.kpi_mappings)
      ? brainProfile.kpi_mappings.map((m) => ({
          kpi_name_cn: m.kpi_name_cn || m.kpi_name || "",
          aliases: m.aliases || "",
          unit: m.unit || "",
        }))
      : [];
    brainGlossary = (gl && gl.glossary) || [];
    brainPrefs = (pf && pf.preferences) || [];
    brainBranding = (br && br.branding) || {};
    brainJobs = (jb && jb.jobs) || [];
    brainReports = (rp && rp.reports) || [];
    if (!brainJobDraft) brainJobDraft = defaultJobDraft();
    brainSeededFor = currentAccount.id;
  } catch (e) {
    brainError = "载入公司记忆失败，请稍后重试。";
    brainProfile = brainProfile || {};
    brainSeededFor = currentAccount.id; // avoid a reload loop; show the error + empty form
  } finally {
    brainLoading = false;
    if (detailOpen && currentView() === "brain") forceRefreshDetail();
  }
}

// (Re)attach the Company Brain page's controls. A no-op on every other page (the
// querySelectors simply find nothing), so it's safe to call from wireDetailBody.
function wireBrainBody(body) {
  // Profile + KPI saves — both persist the WHOLE form, so neither section's edits
  // are lost regardless of which Save you click (the server merges by key anyway).
  const saveProfile = body.querySelector("#brainSaveProfile");
  if (saveProfile) saveProfile.addEventListener("click", saveBrain);
  const saveKpi = body.querySelector("#brainSaveKpi");
  if (saveKpi) saveKpi.addEventListener("click", saveBrain);

  // KPI add / remove rows (capture current inputs first so typing isn't lost).
  const addKpi = body.querySelector("#brainAddKpi");
  if (addKpi) addKpi.addEventListener("click", () => {
    captureBrainForm();
    brainKpiDraft.push({ kpi_name_cn: "", aliases: "", unit: "" });
    brainMsg = ""; forceRefreshDetail();
  });
  body.querySelectorAll(".kpi-rm").forEach((btn) =>
    btn.addEventListener("click", () => {
      captureBrainForm();
      const i = Number(btn.dataset.i);
      if (i >= 0 && i < brainKpiDraft.length) brainKpiDraft.splice(i, 1);
      brainMsg = ""; forceRefreshDetail();
    })
  );

  // Glossary: add a new term / save an edited row / delete a row.
  const addGloss = body.querySelector("#brainAddGloss");
  if (addGloss) addGloss.addEventListener("click", addGlossaryRow);
  body.querySelectorAll(".gloss-save").forEach((btn) =>
    btn.addEventListener("click", () => saveGlossaryRow(btn.dataset.id)));
  body.querySelectorAll(".gloss-del").forEach((btn) =>
    btn.addEventListener("click", () => deleteGlossaryRow(btn.dataset.id)));

  // Preferences: add / toggle on-off / delete.
  const addPref = body.querySelector("#brainAddPref");
  if (addPref) addPref.addEventListener("click", addBrainPref);
  body.querySelectorAll(".pref-toggle").forEach((cb) =>
    cb.addEventListener("change", () => toggleBrainPref(cb.dataset.id, cb.checked)));
  body.querySelectorAll(".pref-del").forEach((btn) =>
    btn.addEventListener("click", () => deleteBrainPref(btn.dataset.id)));

  // Historical import flow.
  const impOpen = body.querySelector("#brainImportOpen");
  if (impOpen) impOpen.addEventListener("click", () => {
    brainImport = { open: true, files: [], estimate: null, estimating: false,
                    starting: false, jobId: null, job: null, error: "" };
    forceRefreshDetail();
  });
  const impFiles = body.querySelector("#brainImportFiles");
  if (impFiles) impFiles.addEventListener("change", () => {
    if (!brainImport) return;
    brainImport.files = Array.from(impFiles.files || []).slice(0, BRAIN_IMPORT_MAX);
    brainImport.estimate = null; // a new pick invalidates the previous estimate
    brainImport.error = "";
    forceRefreshDetail();
  });
  const impEst = body.querySelector("#brainImportEstimate");
  if (impEst) impEst.addEventListener("click", estimateBrainImport);
  const impConfirm = body.querySelector("#brainImportConfirm");
  if (impConfirm) impConfirm.addEventListener("click", startBrainImport);

  // Branding: save (logo + colour + webhook) and keep the colour picker ↔ hex in sync.
  const saveBranding = body.querySelector("#brainSaveBranding");
  if (saveBranding) saveBranding.addEventListener("click", saveBrainBranding);
  const colorPick = body.querySelector("#brandColor");
  const colorHex = body.querySelector("#brandColorHex");
  if (colorPick && colorHex) {
    colorPick.addEventListener("input", () => { colorHex.value = colorPick.value; });
    colorHex.addEventListener("change", () => {
      const v = colorHex.value.trim();
      if (/^#[0-9A-Fa-f]{6}$/.test(v)) colorPick.value = v;
    });
  }

  // Scheduled jobs: preset switch re-renders (to show/hide custom cron); add/toggle/delete.
  const jobPreset = body.querySelector("#jobPreset");
  if (jobPreset) jobPreset.addEventListener("change", () => {
    captureJobDraft();
    brainJobDraft.presetIdx = Number(jobPreset.value);
    brainMsg = ""; forceRefreshDetail();
  });
  const addJob = body.querySelector("#brainAddJob");
  if (addJob) addJob.addEventListener("click", createScheduledJob);
  body.querySelectorAll(".job-toggle").forEach((btn) =>
    btn.addEventListener("click", () => toggleScheduledJob(btn.dataset.id, btn.dataset.active === "1")));
  body.querySelectorAll(".job-del").forEach((btn) =>
    btn.addEventListener("click", () => deleteScheduledJob(btn.dataset.id)));
}

// Read every field on the Company Brain form into state (so an add/remove-row
// re-render, or a Save, never loses what the user has typed).
function captureBrainForm() {
  const body = $("detailBody");
  if (!body) return;
  brainProfile = brainProfile || {};
  const name = body.querySelector("#brainName");
  if (name) brainProfile.company_name = name.value.trim();
  const ind = body.querySelector("#brainIndustry");
  if (ind) brainProfile.industry = ind.value;
  const desc = body.querySelector("#brainDesc");
  if (desc) brainProfile.business_desc = desc.value.trim();
  const fw = body.querySelector('input[name="brainFiscal"]:checked');
  if (fw) brainProfile.fiscal_week_start = fw.value === "Sun" ? "Sun" : "Mon";
  const val = (el) => (el && el.value ? el.value.trim() : "");
  if (body.querySelector(".kpi-table")) {
    brainKpiDraft = Array.from(body.querySelectorAll(".kpi-row")).map((r) => ({
      kpi_name_cn: val(r.querySelector(".kpi-name")),
      aliases: val(r.querySelector(".kpi-aliases")),
      unit: val(r.querySelector(".kpi-unit")),
    }));
  }
}

// Save 公司档案 + 指标定义 together (one POST; the server merges by key).
async function saveBrain() {
  captureBrainForm();
  brainError = ""; brainMsg = "";
  const kpis = brainKpiDraft.filter((m) => m.kpi_name_cn || m.aliases || m.unit);
  const payload = {
    company_name: brainProfile.company_name || "",
    industry: brainProfile.industry || "",
    business_desc: brainProfile.business_desc || "",
    fiscal_week_start: brainProfile.fiscal_week_start || "Mon",
    kpi_mappings: kpis,
  };
  try {
    const res = await fetch("/api/company-brain/profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "保存失败。");
    brainProfile = data.profile || brainProfile;
    brainKpiDraft = Array.isArray(brainProfile.kpi_mappings)
      ? brainProfile.kpi_mappings.map((m) => ({
          kpi_name_cn: m.kpi_name_cn || "", aliases: m.aliases || "", unit: m.unit || "",
        })) : [];
    brainMsg = "已保存。";
  } catch (e) {
    brainError = e.message;
  }
  forceRefreshDetail();
}

// Branding — multipart save (logo file + colour + webhook). Not JSON, so it can't
// use brainPost; it posts FormData directly and refreshes the branding state.
async function saveBrainBranding() {
  const body = $("detailBody");
  if (!body) return;
  brainError = ""; brainMsg = "";
  const fd = new FormData();
  const hex = body.querySelector("#brandColorHex");
  if (hex && hex.value.trim()) fd.append("brand_color", hex.value.trim());
  const wh = body.querySelector("#brandWebhook");
  if (wh) fd.append("wechat_webhook", wh.value.trim());
  const logo = body.querySelector("#brandLogo");
  if (logo && logo.files && logo.files[0]) fd.append("logo", logo.files[0]);
  try {
    const res = await fetch("/api/company-brain/branding", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "保存失败。");
    brainBranding = data.branding || brainBranding;
    brainMsg = "品牌设置已保存。";
  } catch (e) {
    brainError = e.message;
  }
  forceRefreshDetail();
}

// Read the "new 定时任务" form into brainJobDraft so a preset-driven re-render (which
// toggles the custom-cron row) never loses what the user has typed.
function captureJobDraft() {
  const body = $("detailBody");
  if (!body) return;
  const d = brainJobDraft || defaultJobDraft();
  const name = body.querySelector("#jobName");
  if (name) d.name_cn = name.value.trim();
  const preset = body.querySelector("#jobPreset");
  if (preset) d.presetIdx = Number(preset.value);
  const cron = body.querySelector("#jobCron");
  if (cron) d.cron = cron.value.trim();
  const goal = body.querySelector("#jobGoal");
  if (goal) d.goal_template = goal.value.trim();
  const budget = body.querySelector("#jobBudget");
  if (budget) d.budget_cny = budget.value.trim() === "" ? null : Number(budget.value);
  d.channels = Array.from(body.querySelectorAll(".job-chan:checked")).map((c) => c.value);
  brainJobDraft = d;
}

function createScheduledJob() {
  captureJobDraft();
  const d = brainJobDraft;
  brainError = ""; brainMsg = "";
  if (!d.name_cn) { brainError = "请填写任务名称。"; forceRefreshDetail(); return; }
  const preset = FREQ_PRESETS[d.presetIdx] || FREQ_PRESETS[0];
  const cron = preset.cron || (d.cron || "");
  if (!cron) { brainError = "请填写自定义 cron 表达式。"; forceRefreshDetail(); return; }
  const budget = (d.budget_cny == null || d.budget_cny === "" || isNaN(d.budget_cny))
    ? null : Number(d.budget_cny);
  brainPost("/api/scheduled-jobs", {
    name_cn: d.name_cn, cron_expr: cron, goal_template: d.goal_template || "",
    delivery_channels: d.channels || [], source_mode: "latest_upload", budget_cny: budget,
  }, (data) => {
    brainJobs = data.jobs || brainJobs;
    brainJobDraft = defaultJobDraft();
    brainError = ""; brainMsg = "定时任务已创建。";
  });
}

function toggleScheduledJob(id, active) {
  brainPost("/api/scheduled-jobs/update", { id: Number(id), active: !active },
    (data) => { brainJobs = data.jobs || brainJobs; brainError = ""; brainMsg = "已更新。"; });
}

function deleteScheduledJob(id) {
  brainPost("/api/scheduled-jobs/delete", { id: Number(id) },
    (data) => { brainJobs = data.jobs || brainJobs; brainError = ""; brainMsg = "定时任务已删除。"; });
}

// A tiny POST helper for the glossary/preferences CRUD: posts JSON, calls onOk with
// the parsed body on success (or records brainError), then re-renders the page.
async function brainPost(url, payload, onOk) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "操作失败。");
    if (onOk) onOk(data);
  } catch (e) {
    brainError = e.message; brainMsg = "";
  }
  forceRefreshDetail();
}

function addGlossaryRow() {
  const body = $("detailBody");
  if (!body) return;
  const termEl = body.querySelector("#brainGlossTerm");
  const defEl = body.querySelector("#brainGlossDef");
  const term = termEl ? termEl.value.trim() : "";
  const definition = defEl ? defEl.value.trim() : "";
  if (!term) { brainError = "请填写术语。"; brainMsg = ""; forceRefreshDetail(); return; }
  brainPost("/api/company-brain/glossary", { term, definition }, (data) => {
    brainGlossary = data.glossary || brainGlossary; brainError = ""; brainMsg = "术语已添加。";
  });
}

function saveGlossaryRow(id) {
  const body = $("detailBody");
  const row = body && body.querySelector(`.gloss-row[data-id="${id}"]`);
  if (!row) return;
  const termEl = row.querySelector(".gloss-term");
  const defEl = row.querySelector(".gloss-def");
  brainPost("/api/company-brain/glossary/update",
    { id: Number(id), term: termEl ? termEl.value.trim() : "", definition: defEl ? defEl.value.trim() : "" },
    (data) => { brainGlossary = data.glossary || brainGlossary; brainError = ""; brainMsg = "术语已更新。"; });
}

function deleteGlossaryRow(id) {
  brainPost("/api/company-brain/glossary/delete", { id: Number(id) },
    (data) => { brainGlossary = data.glossary || brainGlossary; brainError = ""; brainMsg = "术语已删除。"; });
}

function addBrainPref() {
  const body = $("detailBody");
  const el = body && body.querySelector("#brainPrefInput");
  const instruction = el ? el.value.trim() : "";
  if (!instruction) { brainError = "请填写偏好说明。"; brainMsg = ""; forceRefreshDetail(); return; }
  brainPost("/api/company-brain/preferences", { instruction },
    (data) => { brainPrefs = data.preferences || brainPrefs; brainError = ""; brainMsg = "偏好已添加。"; });
}

function toggleBrainPref(id, active) {
  brainPost("/api/company-brain/preferences/toggle", { id: Number(id), active: !!active },
    (data) => { brainPrefs = data.preferences || brainPrefs; brainError = ""; });
}

function deleteBrainPref(id) {
  brainPost("/api/company-brain/preferences/delete", { id: Number(id) },
    (data) => { brainPrefs = data.preferences || brainPrefs; brainError = ""; brainMsg = "偏好已删除。"; });
}

// ---- Historical import (estimate → confirm → poll) ------------------------
async function estimateBrainImport() {
  if (!brainImport) return;
  if (!brainImport.files || !brainImport.files.length) {
    brainImport.error = "请先选择文件。"; forceRefreshDetail(); return;
  }
  brainImport.estimating = true; brainImport.error = ""; forceRefreshDetail();
  try {
    const fd = new FormData();
    brainImport.files.forEach((f) => fd.append("files", f));
    const res = await fetch("/api/company-brain/import/estimate", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "估算失败。");
    brainImport.estimate = data;
  } catch (e) {
    brainImport.error = e.message;
  } finally {
    brainImport.estimating = false;
    forceRefreshDetail();
  }
}

async function startBrainImport() {
  if (!brainImport || !brainImport.files || !brainImport.files.length) return;
  brainImport.starting = true; brainImport.error = ""; forceRefreshDetail();
  try {
    const fd = new FormData();
    brainImport.files.forEach((f) => fd.append("files", f));
    const res = await fetch("/api/company-brain/import", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "导入失败。");
    brainImport.jobId = data.job_id;
    brainImport.job = {
      status: "running", total: brainImport.files.length, done: 0, cost: 0,
      files: brainImport.files.map((f) => ({ name: f.name, status: "pending", metrics: 0 })),
    };
    forceRefreshDetail();
    if (brainImportTimer) clearInterval(brainImportTimer);
    brainImportTimer = setInterval(pollBrainImport, 1000);
  } catch (e) {
    brainImport.starting = false;
    brainImport.error = e.message;
    forceRefreshDetail();
  }
}

async function pollBrainImport() {
  if (!brainImport || !brainImport.jobId) return;
  try {
    const res = await fetch(`/api/company-brain/import/${brainImport.jobId}`);
    const job = await res.json();
    if (!res.ok) throw new Error(job.error || "查询导入进度失败。");
    brainImport.job = job;
    if (job.status === "running") {
      // Update only the progress box in place so other edits aren't disturbed.
      const el = $("detailBody") && $("detailBody").querySelector("#brainImportProgress");
      if (el) el.innerHTML = importProgressHTML();
    } else {
      clearInterval(brainImportTimer); brainImportTimer = null;
      brainImport.starting = false;
      forceRefreshDetail(); // a full re-render to settle the confirm button + final state
    }
  } catch (e) {
    clearInterval(brainImportTimer); brainImportTimer = null;
    brainImport.starting = false;
    brainImport.error = e.message;
    forceRefreshDetail();
  }
}

// ===========================================================================
// LEARNING LOOP — the "AI团队有 N 个名词想和您确认" banner on the result page.
// Reads snap.unknown_terms; one question per term; POSTs answers to the glossary.
// ===========================================================================
function confirmTermsBannerHTML(snap) {
  if (!snap) return "";
  const terms = snap.unknown_terms || [];
  if (!terms.length) return "";
  if (confirmedTermsJob && confirmedTermsJob === snap.job_id) return ""; // already handled
  if (!currentAccount) {
    return `<div class="confirm-banner">
      <div class="cb-title">AI团队有 ${terms.length} 个名词想和您确认</div>
      <p class="cb-sub">登录后即可保存这些名词的定义，团队下次就能看懂。</p>
    </div>`;
  }
  const rows = terms.map((t, i) => `
    <div class="cb-row">
      <label class="cb-term" for="cbTerm${i}">${esc(t)}</label>
      <input class="field-input cb-input" id="cbTerm${i}" data-term="${esc(t)}" type="text" placeholder="请简要说明「${esc(t)}」的含义" />
    </div>`).join("");
  return `<div class="confirm-banner" id="confirmBanner" data-job="${esc(snap.job_id || "")}">
    <div class="cb-title">AI团队有 ${terms.length} 个名词想和您确认</div>
    <p class="cb-sub">这些名词团队暂时无法判断含义。告诉我们一次，之后每次分析都会自动记住。</p>
    <div class="cb-rows">${rows}</div>
    <div class="brain-row-actions">
      <button type="button" id="confirmTermsSubmit" class="run-btn" style="max-width:200px;">保存到术语表</button>
      <button type="button" id="confirmTermsDismiss" class="ghost-btn">暂不处理</button>
    </div>
    <p id="confirmTermsMsg" class="dash-save-msg" hidden></p>
  </div>`;
}

function wireConfirmTerms(body) {
  const submit = body.querySelector("#confirmTermsSubmit");
  if (submit) submit.addEventListener("click", submitConfirmTerms);
  const dismiss = body.querySelector("#confirmTermsDismiss");
  if (dismiss) dismiss.addEventListener("click", () => {
    const banner = body.querySelector("#confirmBanner");
    confirmedTermsJob = banner ? banner.dataset.job : (lastSnapshot && lastSnapshot.job_id);
    forceRefreshDetail();
  });
}

async function submitConfirmTerms() {
  const body = $("detailBody");
  if (!body) return;
  const banner = body.querySelector("#confirmBanner");
  const jobId = banner ? banner.dataset.job : (lastSnapshot && lastSnapshot.job_id);
  const msg = body.querySelector("#confirmTermsMsg");
  const setMsg = (text, ok) => {
    if (!msg) return;
    msg.textContent = text; msg.hidden = false;
    msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
  };
  const answers = {};
  body.querySelectorAll(".cb-input").forEach((el) => {
    const term = el.dataset.term;
    const def = el.value.trim();
    if (term && def) answers[term] = def;
  });
  if (!Object.keys(answers).length) { setMsg("请至少为一个名词填写定义。", false); return; }
  const submit = body.querySelector("#confirmTermsSubmit");
  if (submit) { submit.disabled = true; submit.textContent = "保存中…"; }
  try {
    const res = await fetch("/api/glossary/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "保存失败。");
    confirmedTermsJob = jobId;  // hide the banner for this job from now on
    brainSeededFor = null;      // the glossary changed — reload the brain next time it opens
    setMsg(`已保存 ${data.written} 个名词到术语表，下次分析团队就能看懂了。`, true);
    setTimeout(() => { if (detailOpen && currentView() === "chatbot") forceRefreshDetail(); }, 1400);
  } catch (e) {
    setMsg(e.message, false);
    if (submit) { submit.disabled = false; submit.textContent = "保存到术语表"; }
  }
}

// ===========================================================================
// FEEDBACK LOOP (Phase 6) — per-section 👍/👎 + comment on the final report,
// and a one-click "存为长期偏好" for 👎 comments that start with "以后…".
// ===========================================================================
function wireFeedbackBars(body) {
  body.querySelectorAll(".fb-bar").forEach((bar) => {
    const votes = bar.querySelectorAll(".fb-vote");
    votes.forEach((b) => b.addEventListener("click", () => {
      votes.forEach((x) => x.classList.remove("selected"));
      b.classList.add("selected");
    }));
    const submit = bar.querySelector(".fb-submit");
    if (submit) submit.addEventListener("click", () => submitSectionFeedback(bar));
  });
}

async function submitSectionFeedback(bar) {
  const sel = bar.querySelector(".fb-vote.selected");
  const msg = bar.querySelector(".fb-msg");
  const setMsg = (t, ok) => {
    if (!msg) return;
    msg.textContent = t; msg.hidden = false;
    msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
  };
  if (!sel) { setMsg("请选择 👍 或 👎。", false); return; }
  const commentEl = bar.querySelector(".fb-comment");
  const payload = {
    run_id: bar.dataset.run || null,
    section_title: bar.dataset.section || "",
    vote: sel.dataset.vote,
    comment: commentEl ? commentEl.value.trim() : "",
  };
  const submit = bar.querySelector(".fb-submit");
  if (submit) { submit.disabled = true; submit.textContent = "提交中…"; }
  try {
    const res = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "提交失败。");
    setMsg("已记录，谢谢！", true);
    if (submit) submit.textContent = "已提交 ✓";
    // A 👎 + "以后…" comment can become a STANDING preference — offer one click.
    if (data.preference_candidate && data.id && !bar.querySelector(".fb-savepref")) {
      const save = document.createElement("button");
      save.type = "button";
      save.className = "fb-savepref";
      save.textContent = "存为长期偏好";
      save.addEventListener("click", () => saveFeedbackPreference(data.id, save, setMsg));
      bar.appendChild(save);
    }
  } catch (e) {
    setMsg(e.message, false);
    if (submit) { submit.disabled = false; submit.textContent = "提交"; }
  }
}

async function saveFeedbackPreference(id, btn, setMsg) {
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    const res = await fetch("/api/feedback/save-preference", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "保存失败。");
    btn.textContent = "已存为长期偏好 ✓";
    if (Array.isArray(data.preferences)) brainPrefs = data.preferences; // keep brain page in sync
    if (setMsg) setMsg("已存为长期偏好，下次报告会自动应用。", true);
  } catch (e) {
    btn.disabled = false; btn.textContent = "存为长期偏好";
    if (setMsg) setMsg(e.message, false);
  }
}

// ===========================================================================
// Phase 10 — onboarding wizard, 新功能 badge, floating 联系支持
// ===========================================================================

// ---- 新功能 badge ----------------------------------------------------------
// /api/changelog/state says whether the newest CHANGELOG entry is unseen by the
// logged-in account. Visiting /changelog (a server page) marks it seen, so the
// badge clears on the next page load — exactly the acceptance behaviour.
async function loadChangelogState() {
  try {
    const res = await fetch("/api/changelog/state");
    const data = await res.json();
    changelogHasNew = !!(res.ok && data && data.has_new);
  } catch (e) {
    changelogHasNew = false;
  }
  const badge = $("menuChangelogBadge");
  if (badge) badge.hidden = !changelogHasNew;
  const dot = $("menuBtnDot");
  if (dot) dot.hidden = !changelogHasNew;
}

// ---- onboarding wizard ------------------------------------------------------
const OB_STEP_TITLES = {
  1: "公司信息", 2: "上传数据", 3: "KPI 确认", 4: "生成首份报告", 5: "每周定时",
};

// A per-account, per-browser "don't auto-open again" mark, set by the × button.
// The wizard stays reachable from the menu; skipping step 5 ends it for real.
function obDismissKey() {
  return "ob-dismissed-" + ((currentAccount && currentAccount.id) || "anon");
}

async function maybeAutoOpenOnboarding() {
  if (!currentAccount || obOpen || detailOpen) return;
  let dismissed = false;
  try { dismissed = localStorage.getItem(obDismissKey()) === "1"; } catch (e) { /* private mode */ }
  if (dismissed) return;
  try {
    const res = await fetch("/api/onboarding/state");
    if (!res.ok) return;
    const st = await res.json();
    obState = st;
    if (!st.done) openOnboarding();
  } catch (e) { /* the wizard stays reachable from the menu */ }
}

function wireOnboarding() {
  const close = $("onboardClose");
  if (close) close.addEventListener("click", () => {
    try { localStorage.setItem(obDismissKey(), "1"); } catch (e) { /* fine */ }
    closeOnboarding();
  });
}

async function openOnboarding(opts) {
  if (!currentAccount) { openLoginModal(); return; }
  obError = ""; obBusy = false;
  try {
    // /start returns the state AND records the funnel's "wizard opened" event.
    const res = await fetch("/api/onboarding/start", { method: "POST" });
    if (res.ok) obState = await res.json();
  } catch (e) { /* fall through with whatever state we have */ }
  if (!obState) obState = { steps: {}, next_step: 1, done: false };
  obStep = (opts && opts.demo) ? 2 : (obState.next_step || 1);
  obJobId = null; obSnap = null; obData = null; obScheduleDone = null;
  const v = $("onboardView");
  if (!v) return;
  obOpen = true;
  v.classList.add("open");
  v.setAttribute("aria-hidden", "false");
  renderOnboard();
  if (opts && opts.demo) obDemoData();   // the email deep link: load the demo now
}

function closeOnboarding() {
  const v = $("onboardView");
  if (v) { v.classList.remove("open"); v.setAttribute("aria-hidden", "true"); }
  obOpen = false;
  if (obPollTimer) { clearInterval(obPollTimer); obPollTimer = null; }
}

function obStepsBarHTML() {
  const st = (obState && obState.steps) || {};
  return [1, 2, 3, 4, 5].map((n) => {
    const done = st[String(n)] === "completed" || st[String(n)] === "skipped";
    const cls = n === obStep ? "active" : (done ? "done" : "");
    return `<span class="ob-step ${cls}">${n}. ${OB_STEP_TITLES[n]}${done ? " ✓" : ""}</span>`;
  }).join("");
}

function renderOnboard() {
  const bar = $("obSteps");
  if (bar) bar.innerHTML = obStepsBarHTML();
  const body = $("onboardBody");
  if (!body) return;
  const builders = { 1: obStep1HTML, 2: obStep2HTML, 3: obStep3HTML,
                     4: obStep4HTML, 5: obStep5HTML };
  body.innerHTML = (builders[obStep] || obStep1HTML)();
  wireOnboardBody(body);
  body.scrollTop = 0;
}

function obErrHTML() {
  return obError ? `<p class="ob-err">${esc(obError)}</p>` : "";
}

function obSkipBtnHTML(label) {
  return `<button type="button" class="ob-skip" id="obSkipBtn">${esc(label || "跳过这一步")}</button>`;
}

// ---- step bodies ----
function obStep1HTML() {
  const name = (obState && obState.company_name) || "";
  const industry = (obState && obState.industry) || "";
  const opts = INDUSTRIES.map((p) =>
    `<option value="${esc(p.key)}" ${p.key === industry || p.display_name_cn === industry ? "selected" : ""}>${esc(p.display_name_cn)}</option>`).join("");
  return `
    <h2 class="ob-h">第 1 步 · 公司信息</h2>
    <p class="ob-sub">选好行业后，AI 团队会套用对应行业的报告模板包（必备指标、行业基准、报告结构）。</p>
    <div class="ob-card">
      <label class="ob-label" for="obCompany">公司名称</label>
      <input id="obCompany" class="ob-input" type="text" placeholder="例如：杭州示例科技有限公司" value="${esc(name)}">
      <label class="ob-label" for="obIndustry">所属行业</label>
      <select id="obIndustry" class="ob-select">${opts}</select>
      ${obErrHTML()}
      <div class="ob-actions">
        <button id="obNext1" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>保存并继续 →</button>
        ${obSkipBtnHTML()}
      </div>
    </div>`;
}

function obStep2HTML() {
  const files = (obState && obState.data_files) || [];
  const existing = files.length
    ? `<p class="ob-file-note">已有数据：${files.map(esc).join("、")} — 可以直接进入下一步，或重新上传。</p>
       <div class="ob-actions"><button id="obNext2" class="run-btn" type="button">直接下一步 →</button></div>`
    : "";
  return `
    <h2 class="ob-h">第 2 步 · 上传一份近期数据文件</h2>
    <p class="ob-sub">Excel（.xlsx）或 CSV 都可以。没有现成数据？用内置示例数据，效果完全一样。</p>
    <div class="ob-choice">
      <div class="ob-card">
        <h3 style="margin:0 0 6px;font-size:16px;">上传我的数据</h3>
        <p class="ob-sub">单文件 ≤ 25MB</p>
        <input id="obFile" type="file" accept=".csv,.xlsx,.xls" class="ob-input">
        <button id="obUploadBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>上传并继续 →</button>
      </div>
      <div class="ob-card">
        <h3 style="margin:0 0 6px;font-size:16px;">用示例数据体验</h3>
        <p class="ob-sub">内置 12 周跨境电商销售数据（${esc((obState && obState.demo_file_name) || "示例销售数据.csv")}）。</p>
        <button id="obDemoBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>${obBusy ? "载入中…" : "用示例数据体验 →"}</button>
      </div>
    </div>
    ${existing}
    ${obErrHTML()}
    <div class="ob-actions">${obSkipBtnHTML()}</div>`;
}

function obStep3HTML() {
  // Prefill: this session's fresh guesses, else the mappings already saved.
  const rows = (obData && obData.kpi_guesses)
    || (obState && obState.kpi_mappings) || [];
  const tr = (r, i) => `
    <tr>
      <td><input data-ob-kpi="name" data-i="${i}" value="${esc(r.kpi_name_cn || "")}"></td>
      <td><input data-ob-kpi="aliases" data-i="${i}" value="${esc(r.aliases || "")}"></td>
      <td><input data-ob-kpi="unit" data-i="${i}" value="${esc(r.unit || "")}" style="width:70px"></td>
    </tr>`;
  const table = rows.length
    ? `<table class="ob-table"><thead><tr><th>指标</th><th>对应的列名（别名，逗号分隔）</th><th>单位</th></tr></thead>
       <tbody>${rows.map(tr).join("")}</tbody></table>`
    : `<p class="ob-sub">（还没有读到表头 — 可先返回上一步上传数据，或直接跳过。）</p>`;
  const fileLine = obData && obData.file_name
    ? `<p class="ob-file-note">来自文件：${esc(obData.file_name)}</p>` : "";
  return `
    <h2 class="ob-h">第 3 步 · KPI 列名确认</h2>
    <p class="ob-sub">系统已按表头猜好每列对应的指标，确认（可修改）后，AI 团队每次分析都会先按这份映射对齐列名。</p>
    <div class="ob-card">
      ${fileLine}
      ${table}
      ${obErrHTML()}
      <div class="ob-actions">
        <button id="obKpiBtn" class="run-btn" type="button" ${obBusy || !rows.length ? "disabled" : ""}>确认并继续 →</button>
        ${obSkipBtnHTML()}
      </div>
    </div>`;
}

function obStep4HTML() {
  const status = obSnap ? obSnap.status : null;
  const acts = (obSnap && obSnap.activity) || [];
  const feed = acts.length
    ? `<ul class="ob-feed" id="obFeed">${acts.map(feedRowHTML).join("")}</ul>` : "";
  let action;
  if (status === "done") {
    action = `<p class="ob-ok">✅ 第一份报告已生成！</p>
      <div class="ob-actions">
        <button id="obViewReport" class="run-btn" type="button">查看完整报告</button>
        <button id="obNext4" class="run-btn" type="button">下一步：设置每周定时 →</button>
      </div>`;
  } else if (status === "error") {
    action = `<p class="ob-err">生成出错：${esc((obSnap && obSnap.error) || "未知错误")}</p>
      <div class="ob-actions">
        <button id="obGenBtn" class="run-btn" type="button">重试</button>
        ${obSkipBtnHTML()}
      </div>`;
  } else if (obJobId) {
    action = `<p class="ob-sub">AI 团队正在工作… 可以实时看到每位专家的进展。</p>`;
  } else {
    action = `${obErrHTML()}
      <div class="ob-actions">
        <button id="obGenBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>${obBusy ? "启动中…" : "生成第一份报告"}</button>
        ${obSkipBtnHTML()}
      </div>
      <p class="ob-file-note">将使用你最近一次上传/示例的数据；费用按模型用量从账户余额扣除。</p>`;
  }
  return `
    <h2 class="ob-h">第 4 步 · 自动生成第一份报告</h2>
    <p class="ob-sub">这是见证时刻：CEO 智能体规划任务、分析师读数、报告员成稿 — 全程直播。</p>
    <div class="ob-card">
      ${action}
      ${feed}
    </div>`;
}

function obStep5HTML() {
  if (obScheduleDone) {
    return `
      <h2 class="ob-h">完成 🎉</h2>
      <div class="ob-card ob-done">
        <h3>每周一早上 8 点，周报自动送达</h3>
        <p class="ob-sub">已创建定时任务「每周经营周报」（${(obScheduleDone.channels || []).map(channelLabel).map(esc).join(" + ") || "邮箱"}）。
        随时可在 企业大脑 → 定时任务 里调整。</p>
        <div class="ob-actions" style="justify-content:center">
          <button id="obFinish" class="run-btn" type="button">开始使用</button>
          <a class="ob-skip" href="/help">看看帮助中心</a>
        </div>
      </div>`;
  }
  return `
    <h2 class="ob-h">第 5 步 · 设置每周定时 + 交付方式</h2>
    <p class="ob-sub">一键创建「每周一早上 8 点」自动生成任务，用最近上传的数据出报告并自动送达。</p>
    <div class="ob-card">
      <label class="ob-check"><input type="checkbox" id="obChEmail" checked> 邮箱（账号邮箱）</label>
      <label class="ob-check"><input type="checkbox" id="obChWechat"> 企业微信群机器人
        <span class="ob-file-note" style="margin:0">（需先在 企业大脑 → 品牌 里配置 webhook）</span></label>
      ${obErrHTML()}
      <div class="ob-actions">
        <button id="obSchedBtn" class="run-btn" type="button" ${obBusy ? "disabled" : ""}>${obBusy ? "创建中…" : "一键开启每周定时 →"}</button>
        ${obSkipBtnHTML("跳过，结束引导")}
      </div>
    </div>`;
}

// ---- step actions ----
function wireOnboardBody(body) {
  const on = (id, fn) => { const el = body.querySelector("#" + id); if (el) el.addEventListener("click", fn); };
  on("obNext1", obSaveCompany);
  on("obUploadBtn", obUpload);
  on("obDemoBtn", obDemoData);
  on("obNext2", () => { obStep = 3; obError = ""; renderOnboard(); });
  on("obKpiBtn", obConfirmKpi);
  on("obGenBtn", obGenerate);
  on("obViewReport", () => {
    // Hand the finished run to the Chat bot workspace (it renders lastSnapshot).
    if (obSnap) { lastSnapshot = obSnap; lastReport = obSnap.final_report || ""; }
    closeOnboarding();
    openDetail("chatbot");
  });
  on("obNext4", () => { obStep = 5; obError = ""; renderOnboard(); });
  on("obSchedBtn", obSchedule);
  on("obFinish", closeOnboarding);
  const skip = body.querySelector("#obSkipBtn");
  if (skip) skip.addEventListener("click", obSkipStep);
}

async function obPost(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "操作失败，请重试。");
  return data;
}

async function obSkipStep() {
  const step = obStep;
  try {
    obState = await obPost("/api/onboarding/skip", { step });
  } catch (e) { /* even if the event write fails, let the user move on */ }
  obError = "";
  if (step >= 5) { closeOnboarding(); return; }
  obStep = step + 1;
  renderOnboard();
}

async function obSaveCompany() {
  const name = ($("obCompany") ? $("obCompany").value : "").trim();
  const industry = $("obIndustry") ? $("obIndustry").value : "";
  obError = ""; obBusy = true; renderOnboard();
  try {
    obState = await obPost("/api/onboarding/company",
                           { company_name: name, industry });
    obStep = 2;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

async function obUpload() {
  const input = $("obFile");
  const f = input && input.files && input.files[0];
  if (!f) { obError = "请先选择一个文件。"; renderOnboard(); return; }
  obError = ""; obBusy = true; renderOnboard();
  try {
    const fd = new FormData();
    fd.append("file", f);
    const res = await fetch("/api/onboarding/upload", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "上传失败。");
    obData = data; obState = data;
    obStep = 3;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

async function obDemoData() {
  obError = ""; obBusy = true; renderOnboard();
  try {
    const data = await obPost("/api/onboarding/demo-data");
    obData = data; obState = data;
    obStep = 3;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

function obCollectKpiRows() {
  const rows = {};
  document.querySelectorAll("#onboardBody [data-ob-kpi]").forEach((el) => {
    const i = el.dataset.i;
    rows[i] = rows[i] || {};
    rows[i][el.dataset.obKpi] = el.value;
  });
  return Object.keys(rows).sort((a, b) => a - b).map((i) => ({
    kpi_name_cn: (rows[i].name || "").trim(),
    aliases: (rows[i].aliases || "").trim(),
    unit: (rows[i].unit || "").trim(),
  })).filter((r) => r.kpi_name_cn || r.aliases);
}

async function obConfirmKpi() {
  obError = ""; obBusy = true;
  const mappings = obCollectKpiRows();
  renderOnboard();
  try {
    obState = await obPost("/api/onboarding/kpi-confirm", { kpi_mappings: mappings });
    obStep = 4;
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

async function obGenerate() {
  obError = ""; obBusy = true; obSnap = null; renderOnboard();
  try {
    const data = await obPost("/api/onboarding/generate");
    obJobId = data.job_id;
    obBusy = false;
    renderOnboard();
    obPollTimer = setInterval(obPollJob, 900);
    obPollJob();
  } catch (e) {
    obError = e.message;
    obBusy = false;
    renderOnboard();
  }
}

async function obPollJob() {
  if (!obJobId) return;
  try {
    const res = await fetch(`/api/status/${obJobId}`);
    const snap = await res.json();
    if (!res.ok) throw new Error(snap.error || "与服务器失去联系。");
    const before = obSnap ? ((obSnap.activity || []).length + obSnap.status) : "";
    obSnap = snap;
    if (snap.status === "done" || snap.status === "error") {
      clearInterval(obPollTimer); obPollTimer = null;
      // Refresh the resumable state — the server records step 4 on success.
      try { obState = await (await fetch("/api/onboarding/state")).json(); } catch (e) { /* fine */ }
      renderOnboard();
      return;
    }
    const after = (snap.activity || []).length + snap.status;
    if (obOpen && obStep === 4 && before !== after) {
      renderOnboard();
      const feed = $("obFeed");
      if (feed) feed.scrollTop = feed.scrollHeight;   // follow the live feed
    }
  } catch (e) {
    clearInterval(obPollTimer); obPollTimer = null;
    obError = e.message; obJobId = null;
    renderOnboard();
  }
}

async function obSchedule() {
  const channels = [];
  if (!$("obChEmail") || $("obChEmail").checked) channels.push("email");
  if ($("obChWechat") && $("obChWechat").checked) channels.push("wechat");
  obError = ""; obBusy = true; renderOnboard();
  try {
    const data = await obPost("/api/onboarding/schedule", { channels });
    obState = data;
    obScheduleDone = { job_id: data.job_id, channels: data.channels || channels };
  } catch (e) {
    obError = e.message;
  }
  obBusy = false;
  renderOnboard();
}

// ---- floating 联系支持 -------------------------------------------------------
function wireSupport() {
  const fab = $("supportFab");
  const pop = $("supportPop");
  if (!fab || !pop) return;
  const email = (META && META.support_email) || "";
  const qrUrl = (META && META.support_qr_url) || "";
  const qr = qrUrl
    ? `<img class="support-qr-img" src="${esc(qrUrl)}" alt="企业微信群二维码">`
    : `<div class="support-qr-ph">企业微信群二维码<br>(待配置)</div>`;
  const mail = email
    ? `<p class="support-mail">邮件：<a href="mailto:${esc(email)}">${esc(email)}</a></p>` : "";
  pop.innerHTML = `
    <h3>联系支持</h3>
    <p class="ob-file-note" style="margin:0 0 4px">扫码加入企业微信支持群：</p>
    ${qr}
    ${mail}
    <textarea id="supportMsg" placeholder="或者在这里留言，我们会尽快回复…"></textarea>
    <input id="supportEmail" type="email" placeholder="你的邮箱（选填，便于回复）">
    <button class="support-send" id="supportSend" type="button">发送留言</button>
    <div class="support-ok" id="supportOk" hidden>已收到，我们会尽快联系你。</div>`;
  fab.hidden = false;
  fab.addEventListener("click", () => pop.classList.toggle("open"));
  const send = $("supportSend");
  if (send) send.addEventListener("click", async () => {
    const msg = ($("supportMsg").value || "").trim();
    if (!msg) return;
    send.disabled = true; send.textContent = "发送中…";
    try {
      await obPost("/api/support", {
        message: msg,
        email: ($("supportEmail").value || "").trim(),
        page: location.pathname + "#spa",
      });
      $("supportOk").hidden = false;
      $("supportMsg").value = "";
    } catch (e) {
      $("supportOk").hidden = false;
      $("supportOk").textContent = e.message;
    }
    send.disabled = false; send.textContent = "发送留言";
  });
}
