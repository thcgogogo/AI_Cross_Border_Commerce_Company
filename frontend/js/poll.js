// ===========================================================================
// poll.js — shared live-feed poller for the file-upload workflows.
//
// The 跨境财务 sub-tabs (回款核对 / 应收账款, and the later engines) all start a
// background run and watch /api/status/<jobId> the same way. This is that one
// poll loop, extracted from recon.js + ar.js so the sub-tabs share it verbatim.
// ===========================================================================
import * as api from "./api.js";
import { esc } from "./components.js";

// Poll /api/status until the run finishes; render the last 8 activity lines into
// `feedEl` (reusing the .recon-feed-line styling). Calls done(true) on success and
// done(false) on error. Returns the interval id so the caller clears it on teardown.
// `errorText` is the fallback shown when a run errors with no error string.
export function pollJobStatus(jobId, feedEl, done, { errorText = "处理失败。" } = {}) {
  const tick = async () => {
    let snap;
    try { snap = await api.getJobStatus(jobId); } catch (e) { return; }
    const acts = (snap.activity || []).slice(-8);
    feedEl.innerHTML = acts.map((a) =>
      `<div class="recon-feed-line ${esc(a.type || "info")}">${esc(a.message || "")}</div>`).join("");
    if (snap.status === "done") { clearInterval(timer); done(true); }
    else if (snap.status === "error") {
      clearInterval(timer);
      feedEl.innerHTML += `<div class="recon-feed-line error">${esc(snap.error || errorText)}</div>`;
      done(false);
    }
  };
  const timer = setInterval(tick, 1500);
  tick();
  return timer;
}
