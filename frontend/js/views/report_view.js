// ===========================================================================
// report_view.js — 报告视图 (/reports/<id>, plan §3.4). A branded DOCUMENT, not
// chat output: brand-color top bar + logo, sticky section TOC, tappable 【N】
// source anchors (bottom sheet on mobile), the 数据来源与核验 appendix with
// 机器核验 badges, per-section 👍/👎 + 存为长期偏好, the UNKNOWN_TERMS confirm
// banner, and a prominent PDF download. Fed by GET /api/reports/<run_id> —
// customer-safe fields only.
// ===========================================================================
import * as api from "../api.js";
import { state } from "../state.js";
import { esc, renderMarkdown, skeleton, statusPillHTML } from "../components.js";

const fbKey = (runId, section) => `tangent-fb-${runId}-${section || "报告开头"}`;
const termsKey = (runId) => `tangent-terms-${runId}`;
const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } };

// ---- helpers ----------------------------------------------------------------
function fmtDateCN(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

// Split the report markdown into sections at #/##/### headings (the feedback
// granularity). The prepended audit-warning block keeps its own entry so it can
// render as a banner instead of a section.
function splitSections(md) {
  const lines = String(md || "").split("\n");
  const sections = [];
  let cur = { title: "", level: 0, lines: [] };
  let started = false;
  const flush = () => {
    if (started || cur.lines.some((l) => l.trim())) {
      sections.push({ title: cur.title, level: cur.level, md: cur.lines.join("\n") });
    }
  };
  for (const line of lines) {
    const m = /^(#{1,3})\s+(.*)$/.exec(line.trim());
    if (m) {
      flush();
      cur = { title: m[2].trim(), level: m[1].length, lines: [line] };
      started = true;
    } else {
      cur.lines.push(line);
    }
  }
  flush();
  return sections.length ? sections : [{ title: "", level: 0, md: String(md || "") }];
}

const isAuditSection = (sec) => (sec.title || "").includes("数据核验提示");

function reportTitle(goalText) {
  const templates = (state.meta && state.meta.report_goal_templates) || [];
  const t = templates.find((x) => x.goal === goalText);
  return t ? t.label : "经营分析报告";
}

// ---- building blocks ----------------------------------------------------------
function feedbackBarHTML(runId, title) {
  const section = title || "报告开头";
  if (lsGet(fbKey(runId, section))) {
    return `<div class="fb-bar fb-done">已收到你的反馈，谢谢 ✓</div>`;
  }
  return `<div class="fb-bar" data-section="${esc(section)}">
      <span class="fb-q">这一节有用吗？</span>
      <button type="button" class="fb-vote" data-vote="up" title="有用" aria-label="有用">👍</button>
      <button type="button" class="fb-vote" data-vote="down" title="需要改进" aria-label="需要改进">👎</button>
      <input type="text" class="fb-comment" placeholder="可选：补充说明，例如「以后把退款放第一节」">
      <button type="button" class="fb-submit">提交</button>
      <span class="fb-msg" hidden></span>
    </div>`;
}

const computedByCN = (v) => (v === "trend_engine"
  ? '<span class="verify-badge">⚙ 机器核验</span>' : "数据分析");

function sourcesHTML(report) {
  const st = report.sources_status;
  const rows = report.sources || [];
  if (st === "ok" && rows.length) {
    const open = !window.matchMedia("(max-width: 720px)").matches;
    const trs = rows.map((s) => `
      <tr id="src-${esc(s.anchor)}">
        <td class="src-anchor">${esc(s.anchor != null ? `【${s.anchor}】` : "—")}</td>
        <td>${esc(s.claim_cn || "—")}</td>
        <td>${esc(s.source_file || "—")}</td>
        <td>${esc(s.source_detail || "—")}</td>
        <td>${computedByCN(s.computed_by)}</td>
      </tr>`).join("");
    return `<details class="sources-block" id="rvSources" ${open ? "open" : ""}>
        <summary>数据来源与核验（${rows.length}）</summary>
        <div class="sources-table-wrap">
          <table class="sources-table">
            <thead><tr><th>锚点</th><th>数据 / 结论</th><th>来源文件</th><th>定位</th><th>核验方式</th></tr></thead>
            <tbody>${trs}</tbody>
          </table>
        </div>
      </details>`;
  }
  if (st && st !== "ok") {
    return `<div class="sources-banner">本次未能生成数据溯源表，报告中的数字请以正文与原始数据为准。</div>`;
  }
  return "";
}

function termsBannerHTML(runId, terms) {
  if (!terms.length || lsGet(termsKey(runId))) return "";
  const rows = terms.map((t, i) => `
    <div class="cb-row">
      <label class="cb-term" for="cbTerm${i}">${esc(t)}</label>
      <input class="field-input cb-input" id="cbTerm${i}" data-term="${esc(t)}" type="text"
        placeholder="请简要说明「${esc(t)}」的含义">
    </div>`).join("");
  return `<div class="confirm-banner" id="rvTerms">
      <div class="cb-title">AI 团队有 ${terms.length} 个名词想和您确认</div>
      <p class="cb-sub">这些名词团队暂时无法判断含义。告诉我们一次，之后每次分析都会自动记住。</p>
      <div class="cb-rows">${rows}</div>
      <div class="cb-actions">
        <button type="button" id="rvTermsSave" class="run-btn">保存到术语表</button>
        <button type="button" id="rvTermsSkip" class="ghost-btn">暂不处理</button>
      </div>
      <p class="form-ok" id="rvTermsMsg" hidden></p>
    </div>`;
}

function unavailableHTML(runId, message, downloadUrl) {
  return `<div class="page page-narrow">
      <div class="empty" style="margin-top:30px;">
        <div class="empty-icon">📄</div>
        <div class="empty-title">报告 #${esc(runId)}</div>
        <p class="empty-lead">${esc(message || "报告内容不可用。")}</p>
        ${downloadUrl ? `<a class="run-btn" style="display:block;max-width:280px;margin:18px auto 0;text-decoration:none;text-align:center;" href="${esc(downloadUrl)}">下载 PDF</a>` : ""}
        <button type="button" class="ghost-btn" id="rvBack" style="max-width:280px;margin:12px auto 0;">返回历史报告</button>
      </div>
    </div>`;
}

// ---- the view -------------------------------------------------------------------
export async function mount(el, ctx) {
  const runId = ctx.params.runId;
  el.innerHTML = `<div class="page page-narrow"><div class="card">${skeleton({ title: true, lines: 6 })}</div></div>`;

  let report;
  try {
    report = await api.getReport(runId);
  } catch (e) {
    const data = e.data || {};
    el.innerHTML = unavailableHTML(runId, data.error || e.message, data.download_url);
    const back = el.querySelector("#rvBack");
    if (back) back.addEventListener("click", () => ctx.navigate("/reports"));
    return;
  }

  // Branding is best-effort decoration — the report must render without it.
  let branding = { brand_color: "", has_logo: false };
  try { branding = (await api.getBrainBranding()).branding || branding; } catch (e) { /* default */ }

  const brandColor = /^#[0-9A-Fa-f]{6}$/.test(branding.brand_color || "") ? branding.brand_color : "#1f5eff";
  const title = reportTitle(report.goal_text);
  const dateLabel = fmtDateCN(report.finished_at || report.started_at);
  const byline = (state.meta && state.meta.product_byline) || "";
  const sections = splitSections(report.final_report || "");
  const tocItems = sections
    .map((s, i) => ({ ...s, i }))
    .filter((s) => s.level === 2 && !isAuditSection(s));

  const bodyHTML = sections.map((sec, i) => {
    if (isAuditSection(sec)) {
      const inner = sec.md.split("\n").slice(1).join("\n"); // banner carries its own title
      return `<div class="audit-banner" id="sec-${i}">
          <div class="ab-title">⚠️ 数据核验提示</div>
          ${renderMarkdown(inner, { anchors: true })}
          <p class="ab-foot">其余数字均已通过自动核验，详见文末「数据来源与核验」。</p>
        </div>`;
    }
    const contentLines = sec.md.split("\n").slice(sec.title ? 1 : 0);
    const hasBody = contentLines.some((l) => l.trim());
    let html = `<section class="report-section" id="sec-${i}">${renderMarkdown(sec.md, { anchors: true })}`;
    if (state.account && hasBody) html += feedbackBarHTML(runId, sec.title);
    return html + "</section>";
  }).join("");

  el.innerHTML = `<div class="page report-page">
    <div class="report-frame" style="--brand:${esc(brandColor)};">
      <div class="report-brandbar">
        ${branding.has_logo ? `<img class="report-logo" src="/api/company-brain/logo" alt="">` : ""}
        <div class="report-id">
          <div class="report-title">${esc(title)}</div>
          <div class="report-date">${esc(dateLabel)}${dateLabel ? " · " : ""}${statusPillHTML(report.status)}</div>
        </div>
        ${report.download_url ? `<a class="brandbar-dl" href="${esc(report.download_url)}">下载 PDF</a>` : ""}
      </div>
      ${tocItems.length ? `<nav class="toc report-toc" id="rvToc" aria-label="报告目录">
        ${tocItems.map((s) => `<a href="#sec-${s.i}" data-sec="${s.i}">${esc(s.title)}</a>`).join("")}
      </nav>` : ""}
      <div class="report-inner">
        ${report.final_report ? `<div class="report-body" id="rvBody">${bodyHTML}</div>`
          : `<div class="empty"><div class="empty-icon">⏳</div>
               <div class="empty-title">报告内容尚未生成</div>
               <p class="empty-lead">这次运行没有产出可在线阅读的内容。${report.has_pdf ? "可尝试下载 PDF。" : ""}</p>
             </div>`}
        ${state.account ? termsBannerHTML(runId, report.unknown_terms || []) : ""}
        ${sourcesHTML(report)}
        <div class="report-actions">
          ${report.download_url
            ? `<a class="run-btn dl-btn" href="${esc(report.download_url)}">⬇ 下载 PDF</a>
               <p class="forward-hint">下载后可直接在企业微信里转发给同事 — 每个数字都可溯源。</p>`
            : `<p class="muted">该报告暂无 PDF 文件。</p>`}
        </div>
      </div>
      <div class="report-foot">${esc(byline)}</div>
    </div>
    <div class="bsheet-backdrop" id="rvSheetBack" hidden></div>
    <div class="bsheet" id="rvSheet" role="dialog" aria-label="数据来源" aria-hidden="true"></div>
  </div>`;

  const $ = (id) => el.querySelector("#" + id);

  // ---- 【N】 anchors → source row (desktop scroll / mobile bottom sheet) ------
  function closeSheet() {
    const sheet = $("rvSheet");
    const back = $("rvSheetBack");
    if (!sheet || !back) return;
    sheet.classList.remove("open");
    back.classList.remove("show");
    sheet.setAttribute("aria-hidden", "true");
    setTimeout(() => { if (!sheet.classList.contains("open")) back.hidden = true; }, 220);
  }

  function openSheet(src) {
    const sheet = $("rvSheet");
    const back = $("rvSheetBack");
    sheet.innerHTML = `
      <div class="bsheet-grip" aria-hidden="true"></div>
      <div class="bsheet-title">数据来源 ${esc(src.anchor != null ? `【${src.anchor}】` : "")}</div>
      <dl class="bsheet-rows">
        <dt>数据 / 结论</dt><dd>${esc(src.claim_cn || "—")}</dd>
        <dt>来源文件</dt><dd>${esc(src.source_file || "—")}</dd>
        <dt>定位</dt><dd>${esc(src.source_detail || "—")}</dd>
        <dt>核验方式</dt><dd>${computedByCN(src.computed_by)}</dd>
      </dl>
      <button type="button" class="ghost-btn bsheet-close">知道了</button>`;
    back.hidden = false;
    requestAnimationFrame(() => { back.classList.add("show"); sheet.classList.add("open"); });
    sheet.setAttribute("aria-hidden", "false");
    sheet.querySelector(".bsheet-close").addEventListener("click", closeSheet);
  }

  function showSource(n) {
    const src = (report.sources || []).find((s) => Number(s.anchor) === n);
    const details = $("rvSources");
    if (!src) { // no parsed row — at least reveal the appendix
      if (details) { details.open = true; details.scrollIntoView({ behavior: "smooth", block: "center" }); }
      return;
    }
    if (window.matchMedia("(max-width: 720px)").matches) { openSheet(src); return; }
    if (details) details.open = true;
    const row = el.querySelector(`#src-${CSS.escape(String(src.anchor))}`);
    if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("flash");
      setTimeout(() => row.classList.remove("flash"), 1600);
    }
  }

  // ---- one delegated click handler for anchors / TOC / feedback ----------------
  el.addEventListener("click", async (e) => {
    const chip = e.target.closest(".anchor-chip");
    if (chip) { showSource(Number(chip.dataset.anchor)); return; }

    const tocLink = e.target.closest("#rvToc a");
    if (tocLink) {
      e.preventDefault();
      const sec = el.querySelector(`#sec-${CSS.escape(tocLink.dataset.sec)}`);
      if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
      el.querySelectorAll("#rvToc a").forEach((a) => a.classList.toggle("active", a === tocLink));
      return;
    }

    if (e.target.closest("#rvSheetBack")) { closeSheet(); return; }

    const vote = e.target.closest(".fb-vote");
    if (vote) {
      const bar = vote.closest(".fb-bar");
      bar.querySelectorAll(".fb-vote").forEach((b) => b.classList.toggle("selected", b === vote));
      return;
    }

    const submit = e.target.closest(".fb-submit");
    if (submit) { await submitFeedback(submit.closest(".fb-bar")); return; }
  });

  async function submitFeedback(bar) {
    const sel = bar.querySelector(".fb-vote.selected");
    const msg = bar.querySelector(".fb-msg");
    const setMsg = (t, ok) => {
      msg.textContent = t; msg.hidden = false;
      msg.classList.toggle("ok", !!ok); msg.classList.toggle("err", !ok);
    };
    if (!sel) { setMsg("请选择 👍 或 👎。", false); return; }
    const btn = bar.querySelector(".fb-submit");
    btn.disabled = true; btn.textContent = "提交中…";
    try {
      const data = await api.sendFeedback({
        run_id: runId,
        section_title: bar.dataset.section || "",
        vote: sel.dataset.vote,
        comment: (bar.querySelector(".fb-comment").value || "").trim(),
      });
      lsSet(fbKey(runId, bar.dataset.section), "1");
      setMsg("已记录，谢谢！", true);
      btn.textContent = "已提交 ✓";
      // A 👎 + "以后…" comment can become a STANDING preference — one click.
      if (data.preference_candidate && data.id && !bar.querySelector(".fb-savepref")) {
        const save = document.createElement("button");
        save.type = "button";
        save.className = "fb-savepref";
        save.textContent = "存为长期偏好";
        save.addEventListener("click", async () => {
          save.disabled = true; save.textContent = "保存中…";
          try {
            await api.saveFeedbackPreference(data.id);
            save.textContent = "已存为长期偏好 ✓";
            setMsg("已存为长期偏好，下次报告会自动应用。", true);
          } catch (err) {
            save.disabled = false; save.textContent = "存为长期偏好";
            setMsg(err.message, false);
          }
        });
        bar.appendChild(save);
      }
    } catch (err) {
      btn.disabled = false; btn.textContent = "提交";
      setMsg(err.message, false);
    }
  }

  // ---- UNKNOWN_TERMS confirm banner ---------------------------------------------
  const termsSave = $("rvTermsSave");
  if (termsSave) {
    termsSave.addEventListener("click", async () => {
      const answers = {};
      el.querySelectorAll(".cb-input").forEach((inp) => {
        const v = (inp.value || "").trim();
        if (v) answers[inp.dataset.term] = v;
      });
      const msg = $("rvTermsMsg");
      if (!Object.keys(answers).length) {
        msg.hidden = false; msg.className = "form-error"; msg.textContent = "请先填写至少一个名词的含义。";
        return;
      }
      termsSave.disabled = true; termsSave.textContent = "保存中…";
      try {
        await api.confirmGlossaryTerms(answers);
        lsSet(termsKey(runId), "1");
        msg.hidden = false; msg.className = "form-ok";
        msg.textContent = "已保存到术语表，AI 团队下次就能看懂这些名词了。";
        termsSave.textContent = "已保存 ✓";
        $("rvTermsSkip").hidden = true;
      } catch (err) {
        termsSave.disabled = false; termsSave.textContent = "保存到术语表";
        msg.hidden = false; msg.className = "form-error"; msg.textContent = err.message;
      }
    });
    $("rvTermsSkip").addEventListener("click", () => {
      lsSet(termsKey(runId), "1");
      $("rvTerms").remove();
    });
  }

  // ---- TOC scroll-spy --------------------------------------------------------------
  const tocEl = $("rvToc");
  let onScroll = null;
  if (tocEl && tocItems.length) {
    onScroll = () => {
      let current = tocItems[0].i;
      for (const s of tocItems) {
        const sec = el.querySelector(`#sec-${s.i}`);
        if (sec && sec.getBoundingClientRect().top <= 130) current = s.i;
      }
      tocEl.querySelectorAll("a").forEach((a) =>
        a.classList.toggle("active", Number(a.dataset.sec) === current));
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  const onKey = (e) => { if (e.key === "Escape") closeSheet(); };
  document.addEventListener("keydown", onKey);

  // Print (Phase 10 B4): CSS cannot reveal a closed <details>, so the sources
  // appendix opens itself when printing starts (mobile loads it collapsed).
  const onBeforePrint = () => { const d = $("rvSources"); if (d) d.open = true; };
  window.addEventListener("beforeprint", onBeforePrint);

  return () => {
    if (onScroll) window.removeEventListener("scroll", onScroll);
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("beforeprint", onBeforePrint);
  };
}
