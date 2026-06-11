// ===========================================================================
// run_feed.js — the shared live "AI 团队工作直播" (plan §3.3), reused by the
// 生成报告 view and the onboarding wizard's step 4.
//
// Customers get the theater, never the machinery (plan §2 rules 1 + 8): the
// token_manager's lines, the context-block confirmations and the length-ceiling
// warnings are filtered out. Founder accounts (is_admin) see the raw feed.
// ===========================================================================
import * as api from "./api.js";
import { isAdmin } from "./state.js";
import { feedRowHTML } from "./components.js";

const MACHINERY_INFO_PREFIXES = [
  "Received COMPANY MEMORY",
  "Received KPI MAPPING",
  "Received INDUSTRY PACK",
];

export function filterActivity(activity, { admin = isAdmin() } = {}) {
  if (admin) return activity || [];
  return (activity || []).filter((a) => {
    if (a.agent === "token_manager") return false;
    const msg = String(a.message || "");
    if (a.type === "info" && MACHINERY_INFO_PREFIXES.some((p) => msg.startsWith(p))) return false;
    if (msg.startsWith("Output hit its length ceiling")) return false;
    return true;
  });
}

export function feedHTML(activity, opts) {
  return filterActivity(activity, opts).map(feedRowHTML).join("");
}

// Poll one job's snapshot every 900ms, repaint `listEl` (filtered, following
// the tail), and report progress upward. Returns stop() — the mounting view
// MUST call it from its cleanup so no timer outlives the page.
export function startRunFeed(jobId, { listEl, onUpdate = null, onDone = null, onError = null } = {}) {
  let lastKey = "";
  let stopped = false;

  const stop = () => { stopped = true; clearInterval(timer); };

  const tick = async () => {
    if (stopped) return;
    let snap;
    try {
      snap = await api.getJobStatus(jobId);
    } catch (e) {
      stop();
      if (onError) onError(e.message, null);
      return;
    }
    const rows = filterActivity(snap.activity);
    const key = `${rows.length}:${snap.status}`;
    if (listEl && key !== lastKey) {
      lastKey = key;
      listEl.innerHTML = rows.map(feedRowHTML).join("");
      listEl.scrollTop = listEl.scrollHeight; // follow the live feed
    }
    if (onUpdate) onUpdate(snap);
    if (snap.status === "done" || snap.status === "error") {
      stop();
      if (snap.status === "done" && onDone) onDone(snap);
      if (snap.status === "error" && onError) onError(snap.error || "未知错误", snap);
    }
  };

  const timer = setInterval(tick, 900);
  tick();
  return stop;
}
