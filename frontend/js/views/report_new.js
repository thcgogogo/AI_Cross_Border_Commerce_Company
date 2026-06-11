// ===========================================================================
// report_new.js — 生成报告 (plan §3.3). Zero-prompt surface: report-type cards
// composed from /api/meta's goal templates + a 自定义分析 card (its 补充说明
// textarea IS the goal), industry from the company profile, drag-drop files,
// one big 开始生成 button, then the live customer-filtered feed. On completion
// it lands on the branded report view at /reports/<run_id>.
// ===========================================================================
import * as api from "../api.js";
import { state, loadIndustries } from "../state.js";
import { esc, fmtBytes, pageHead, quotaMeter, statusPillHTML, renderMarkdown } from "../components.js";
import { startRunFeed } from "../run_feed.js";

// A generic boss-report goal for a 自定义分析 left without 补充说明 — the page
// must stay zero-typing even on the custom card (plan §2 rule 3).
const CUSTOM_FALLBACK_GOAL =
  "基于本次上传的最新经营数据，生成一份面向企业老板的经营分析报告，使用商务汇报体中文："
  + "核心结论、关键指标变化、与历史数据对比、风险与异常提醒、三条建议行动。";

export function mount(el, ctx) {
  const navigate = ctx.navigate;
  let files = [];          // the staged File objects (name/size shown, removable)
  let typeCode = null;     // selected report-type card (template code or "custom")
  let running = false;
  let stopFeed = null;     // the live feed's stop() — cleared in cleanup

  const templates = (state.meta && state.meta.report_goal_templates) || [];
  if (templates.length) typeCode = templates[0].code; // first = default (经营周报)

  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("生成报告", "选择报告类型、拖入数据文件，点一个按钮 — 不用写任何提示词。")}
    <div id="rnForm">
      <div class="card">
        <h3 class="card-title">报告类型</h3>
        <div class="rn-type-grid" id="rnTypes"></div>
        <div id="rnCustomWrap" hidden>
          <label class="field-label" for="rnCustom">补充说明 <span class="field-optional">（可选）</span></label>
          <textarea id="rnCustom" class="field-input" rows="3"
            placeholder="想分析什么？例如：对比各渠道的退货率变化，并给出改进建议。"></textarea>
        </div>
        <label class="field-label" for="rnIndustry">所属行业</label>
        <select id="rnIndustry" class="ob-select"></select>
        <h3 class="card-title" style="margin-top:18px;">数据文件</h3>
        <div class="drop-zone" id="rnDrop" tabindex="0">
          <div class="dz-icon">📂</div>
          <div class="dz-title">拖拽文件到这里，或点击选择</div>
          <div class="dz-sub">Excel（.xlsx）或 CSV 都可以，单文件 ≤ 25MB</div>
          <input id="rnFile" type="file" multiple accept=".csv,.xlsx,.xls" hidden>
        </div>
        <ul class="file-list" id="rnFiles"></ul>
        <div id="rnQuota" class="rn-quota"></div>
        <p class="form-error" id="rnErr" hidden></p>
        <button type="button" class="run-btn" id="rnStart">开始生成</button>
      </div>
    </div>
    <div id="rnLive" hidden>
      <div class="card">
        <div class="rn-live-head">
          <h3 class="card-title" style="margin:0;">AI 团队正在为你工作</h3>
          <span id="rnPill">${statusPillHTML("starting")}</span>
        </div>
        <p class="muted" style="margin:6px 0 0;">全程直播每位专家的进展，完成后自动打开报告。</p>
        <ul class="feed run-feed" id="rnFeed"></ul>
      </div>
      <div id="rnResult"></div>
    </div>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);

  // ---- report-type cards ----------------------------------------------------
  const TYPE_CARDS = templates.map((t) => ({
    code: t.code, label: t.label,
    sub: t.code === "monthly" ? "本月核心结论、环比与趋势对比、下月行动建议"
                              : "本周核心结论、关键指标环比、下周行动建议",
  })).concat([{ code: "custom", label: "自定义分析", sub: "想分析什么都可以，补充一句说明即可" }]);

  function renderTypes() {
    $("rnTypes").innerHTML = TYPE_CARDS.map((c) => `
      <button type="button" class="rn-type ${typeCode === c.code ? "on" : ""}" data-code="${esc(c.code)}">
        <span class="rn-type-name">${esc(c.label)}</span>
        <span class="rn-type-sub">${esc(c.sub)}</span>
      </button>`).join("");
    $("rnCustomWrap").hidden = typeCode !== "custom";
  }
  renderTypes();
  $("rnTypes").addEventListener("click", (e) => {
    const btn = e.target.closest(".rn-type");
    if (!btn) return;
    typeCode = btn.dataset.code;
    renderTypes();
  });

  // ---- industry (prefilled from the company profile, sent as the override) ---
  (async () => {
    const [industries, profile] = await Promise.all([
      loadIndustries(),
      api.getBrainProfile().then((d) => (d && d.profile) || {}).catch(() => ({})),
    ]);
    const sel = $("rnIndustry");
    if (!sel) return; // view unmounted while loading
    const current = (profile.industry || "").trim();
    const opts = (industries || []).map((p) => {
      const selected = p.key === current || p.display_name_cn === current;
      return `<option value="${esc(p.key)}" ${selected ? "selected" : ""}>${esc(p.display_name_cn)}</option>`;
    });
    sel.innerHTML = `<option value="auto">通用（按数据自动判断）</option>` + opts.join("");
  })();

  // ---- quota hint (/api/plans current) ---------------------------------------
  (async () => {
    let cur = null;
    try { cur = (await api.getPlans()).current; } catch (e) { /* hint only */ }
    const box = $("rnQuota");
    if (!box || !cur || !cur.monthly_report_quota) return;
    const used = cur.reports_used_this_month || 0;
    const total = cur.monthly_report_quota;
    if (cur.lifetime_quota) {
      const left = Math.max(0, total - used);
      box.innerHTML = quotaMeter({ used, total, caption: "免费体验额度" })
        + `<p class="muted" style="margin:6px 0 0;">剩余体验 ${left} 份报告。</p>`;
    } else {
      box.innerHTML = quotaMeter({ used, total, caption: "本月已用" });
    }
  })();

  // ---- file zone --------------------------------------------------------------
  function renderFiles() {
    $("rnFiles").innerHTML = files.map((f, i) => `
      <li class="file-item">
        <span class="file-name">${esc(f.name)}</span>
        <span class="file-size">${esc(fmtBytes(f.size))}</span>
        <button type="button" class="file-rm" data-i="${i}" title="移除" aria-label="移除 ${esc(f.name)}">&times;</button>
      </li>`).join("");
  }
  function addFiles(list) {
    for (const f of list || []) {
      if (!f || !f.name) continue;
      if (files.some((x) => x.name === f.name && x.size === f.size)) continue;
      files.push(f);
    }
    renderFiles();
  }
  const drop = $("rnDrop");
  drop.addEventListener("click", () => $("rnFile").click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("rnFile").click(); }
  });
  $("rnFile").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("drag");
  }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove("drag");
  }));
  drop.addEventListener("drop", (e) => addFiles(e.dataTransfer && e.dataTransfer.files));
  $("rnFiles").addEventListener("click", (e) => {
    const btn = e.target.closest(".file-rm");
    if (!btn) return;
    files.splice(Number(btn.dataset.i), 1);
    renderFiles();
  });

  // ---- start the run ------------------------------------------------------------
  function showError(msg, { upsell = false } = {}) {
    const p = $("rnErr");
    p.hidden = false;
    p.innerHTML = esc(msg) + (upsell
      ? ` <a href="/account" data-nav>查看套餐与升级 →</a>` : "");
  }

  function composedGoal() {
    if (typeCode === "custom") {
      const text = ($("rnCustom").value || "").trim();
      return text || CUSTOM_FALLBACK_GOAL;
    }
    const t = templates.find((x) => x.code === typeCode);
    return (t && t.goal) || CUSTOM_FALLBACK_GOAL;
  }

  $("rnStart").addEventListener("click", async () => {
    if (running) return;
    $("rnErr").hidden = true;
    if (!files.length) { showError("请先添加至少一个数据文件（Excel 或 CSV）。"); return; }
    const btn = $("rnStart");
    btn.disabled = true; btn.textContent = "启动中…";
    const fd = new FormData();
    fd.append("goal", composedGoal());
    const industry = $("rnIndustry").value;
    if (industry && industry !== "auto") fd.append("industry", industry);
    files.forEach((f) => fd.append("files", f));
    let jobId;
    try {
      jobId = (await api.startRun(fd)).job_id;
    } catch (e) {
      btn.disabled = false; btn.textContent = "开始生成";
      if (e.status === 402) {
        showError(e.message || "报告额度已用完。升级套餐后即可继续生成。", { upsell: true });
      } else {
        showError(e.message || "启动失败，请稍后重试。");
      }
      return;
    }
    running = true;
    $("rnForm").hidden = true;
    $("rnLive").hidden = false;
    stopFeed = startRunFeed(jobId, {
      listEl: $("rnFeed"),
      onUpdate(snap) { const p = $("rnPill"); if (p) p.innerHTML = statusPillHTML(snap.status); },
      onDone(snap) {
        if (snap.run_id != null && snap.run_id !== "") {
          navigate(`/reports/${snap.run_id}`);
          return;
        }
        // No run row (company brain was unavailable) — show the result inline.
        const box = $("rnResult");
        if (box) {
          box.innerHTML = `<div class="card">
            <h3 class="card-title">✅ 报告已生成</h3>
            <div class="report-body">${renderMarkdown(snap.final_report || "")}</div>
          </div>`;
        }
      },
      onError(msg) {
        running = false;
        $("rnLive").hidden = true;
        $("rnForm").hidden = false;
        const b = $("rnStart");
        b.disabled = false; b.textContent = "开始生成";
        showError("生成出错：" + (msg || "未知错误"));
      },
    });
  });

  return () => { if (stopFeed) stopFeed(); };
}
