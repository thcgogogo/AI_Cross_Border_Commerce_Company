// ===========================================================================
// brain.js — 企业大脑 (plan §3.7, B6): the page that makes Moat 1 visible.
//
// ① Hero stats strip from /api/company-brain/summary — 已记录指标 N 项 ·
//    覆盖 M 个周期 · 术语 K 条 · 长期偏好 P 条 (statTile atoms), or the
//    大脑正在等待第一份报告 empty state when the brain is empty.
// ② KPI 趋势 tiles — latest value, 环比 arrow, streak note, inline SVG
//    sparkline (components.sparklineSVG) per tracked KPI.
// ③ The editors as pill tabs (components.tabsHTML): 公司档案 / 指标定义 /
//    术语表 / 报告偏好 / 品牌 / 历史数据导入. Webhook editing stays in
//    /schedule (品牌 links there). The import tab is the shared
//    import_flow.js (Phase 8 — the onboarding wizard's step ④ reuses it):
//    customers see 文件数 + 预计时间 — never raw ¥ (the API only sends ¥
//    fields to founder sessions).
// mount() returns a cleanup that stops the import progress poll.
// ===========================================================================
import * as api from "../api.js";
import { state, loadIndustries } from "../state.js";
import { esc, fmtNum, pageHead, skeleton, skeletonCard, statTile, sparklineSVG, tabsHTML } from "../components.js";
import { mountImportFlow } from "../import_flow.js";

const TABS = [
  { key: "profile", label: "公司档案" },
  { key: "kpi", label: "指标定义" },
  { key: "glossary", label: "术语表" },
  { key: "prefs", label: "报告偏好" },
  { key: "brand", label: "品牌" },
  { key: "import", label: "历史数据导入" },
];

let stopImport = null;   // the active import flow's stop() (unmount/tab switch)

function stopImportPoll() {
  if (stopImport) { stopImport(); stopImport = null; }
}

// ---------------------------------------------------------------------------
// mount
// ---------------------------------------------------------------------------
export function mount(el, ctx) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("企业大脑", "这是只属于你公司的经营记忆，每份报告都会让它更聪明。")}
    <div id="brainHero">${skeletonCard({ lines: 3, title: true })}</div>
    <div class="card" style="margin-top:14px;">
      <div id="brainTabs">${tabsHTML(TABS, "profile")}</div>
      <div id="brainPanel" class="brain-panel">${skeleton({ lines: 3 })}</div>
    </div>
  </div>`;

  const hero = el.querySelector("#brainHero");
  const tabsBox = el.querySelector("#brainTabs");
  const panel = el.querySelector("#brainPanel");
  let active = "profile";

  refreshHero(hero, ctx);

  tabsBox.addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab || tab.dataset.tab === active) return;
    active = tab.dataset.tab;
    tabsBox.querySelectorAll(".tab").forEach((t) => {
      const on = t.dataset.tab === active;
      t.classList.toggle("on", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    stopImportPoll();
    renderPanel();
  });

  function renderPanel() {
    panel.innerHTML = skeleton({ lines: 3 });
    const render = {
      profile: renderProfileTab,
      kpi: renderKpiTab,
      glossary: renderGlossaryTab,
      prefs: renderPrefsTab,
      brand: renderBrandTab,
      import: renderImportTab,
    }[active];
    render(panel, { onBrainChanged: () => refreshHero(hero, ctx) });
  }
  renderPanel();

  return () => stopImportPoll();
}

// ---------------------------------------------------------------------------
// ① + ② hero: stats strip + KPI trend tiles (or the empty state)
// ---------------------------------------------------------------------------
async function refreshHero(hero, ctx) {
  let summary = null;
  let loadFailed = false;
  try { summary = await api.getBrainSummary(); } catch (e) { loadFailed = true; }
  if (!hero.isConnected) return;

  // A fetch/503 failure is NOT an empty brain — a company with months of data
  // must see a retry state, never the 等待第一份报告 CTA (Phase 7 A3).
  if (loadFailed) {
    hero.innerHTML = `<div class="card"><div class="empty" style="border:none;padding:26px 10px;">
        <div class="empty-icon">⚠️</div>
        <div class="empty-title">暂时无法加载</div>
        <p class="empty-lead">企业大脑数据暂时取不回来，可能是网络波动。稍等片刻再试一次。</p>
        <button type="button" class="run-btn" id="brainRetry">重试</button>
      </div></div>`;
    const retry = hero.querySelector("#brainRetry");
    if (retry) retry.addEventListener("click", () => {
      hero.innerHTML = skeletonCard({ lines: 3, title: true });
      refreshHero(hero, ctx);
    });
    return;
  }

  const s = summary || {};
  const isEmpty = !(s.metrics_count || s.kpi_count || s.glossary_count || s.preferences_count);
  if (isEmpty) {
    hero.innerHTML = `<div class="card"><div class="empty" style="border:none;padding:26px 10px;">
        <div class="empty-icon">🧠</div>
        <div class="empty-title">大脑正在等待第一份报告</div>
        <p class="empty-lead">生成第一份报告后，这里会出现你公司的指标全景与趋势小图 —
          也可以先在下方「历史数据导入」里导入过去 6–12 个月的表格，第一份报告就带趋势对比。</p>
        <button type="button" class="run-btn" id="brainGoNew">去生成第一份报告</button>
      </div></div>`;
    const btn = hero.querySelector("#brainGoNew");
    if (btn) btn.addEventListener("click", () => ctx.navigate("/new"));
    return;
  }

  const kpis = s.kpis || [];
  hero.innerHTML = `<div class="card">
      <div class="brain-stats">
        ${statTile({ value: fmtNum(s.kpi_count), label: "已记录指标（项）" })}
        ${statTile({ value: fmtNum(s.weeks_covered), label: "覆盖周期（个）" })}
        ${statTile({ value: fmtNum(s.glossary_count), label: "术语（条）" })}
        ${statTile({ value: fmtNum(s.preferences_count), label: "长期偏好（条）" })}
      </div>
    </div>
    ${kpis.length ? `<div class="card" style="margin-top:14px;">
      <h3 class="card-title">📈 KPI 趋势</h3>
      <div class="kpi-grid">${kpis.map(kpiTileHTML).join("")}</div>
    </div>` : ""}`;
}

function kpiTileHTML(k) {
  let delta;
  if (k.delta_pct == null || isNaN(k.delta_pct)) {
    delta = `<span class="kpi-delta flat">环比 —</span>`;
  } else {
    const up = Number(k.delta_pct) >= 0;
    delta = `<span class="kpi-delta ${up ? "up" : "down"}">环比 ${up ? "↑" : "↓"} ${esc(Math.abs(Number(k.delta_pct)))}%</span>`;
  }
  const streak = k.streak
    ? `<div class="kpi-streak ${k.streak.direction === "down" ? "down" : "up"}">已连续 ${esc(k.streak.length)} 周${k.streak.direction === "down" ? "下滑" : "上升"}</div>`
    : "";
  const latest = (k.latest == null) ? "—" : fmtNum(k.latest);
  return `<div class="kpi-tile">
      <div class="kpi-name" title="${esc(k.name)}">${esc(k.name)}</div>
      <div class="kpi-val">${esc(latest)}${k.unit ? `<span class="kpi-unit">${esc(k.unit)}</span>` : ""}</div>
      <div class="kpi-meta">${delta}</div>
      ${streak}
      ${sparklineSVG((k.series || []).map((p) => p.value))}
    </div>`;
}

// ---------------------------------------------------------------------------
// ③a 公司档案
// ---------------------------------------------------------------------------
async function renderProfileTab(panel) {
  const [industries, data] = await Promise.all([
    loadIndustries(),
    api.getBrainProfile().then((d) => (d && d.profile) || {}).catch(() => ({})),
  ]);
  if (!panel.isConnected) return;
  const cur = (data.industry || "").trim();
  const opts = (industries || []).map((p) => {
    const selected = p.key === cur || p.display_name_cn === cur;
    return `<option value="${esc(p.key)}" ${selected ? "selected" : ""}>${esc(p.display_name_cn)}</option>`;
  });
  panel.innerHTML = `
    <label class="field-label" for="bpName">公司名称</label>
    <input id="bpName" class="field-input" type="text" value="${esc(data.company_name || "")}" placeholder="例如：星帆贸易">
    <label class="field-label" for="bpIndustry">所属行业</label>
    <select id="bpIndustry" class="ob-select">
      <option value="" ${cur ? "" : "selected"}>通用（按数据自动判断）</option>${opts.join("")}
    </select>
    <label class="field-label" for="bpDesc">业务简介（帮助 AI 团队理解你的生意）</label>
    <textarea id="bpDesc" class="field-input" rows="3" placeholder="例如：跨境电商，主营家居用品，亚马逊 + 独立站双渠道">${esc(data.business_desc || "")}</textarea>
    <label class="field-label" for="bpWeek">财务周起始日</label>
    <select id="bpWeek" class="ob-select">
      <option value="Mon" ${data.fiscal_week_start === "Sun" ? "" : "selected"}>周一</option>
      <option value="Sun" ${data.fiscal_week_start === "Sun" ? "selected" : ""}>周日</option>
    </select>
    <p class="form-error" id="bpErr" hidden></p>
    <p class="sch-ok" id="bpOk" hidden></p>
    <button type="button" class="run-btn" id="bpSave" style="max-width:220px;">保存公司档案</button>`;
  panel.querySelector("#bpSave").addEventListener("click", async () => {
    const ok = panel.querySelector("#bpOk");
    const err = panel.querySelector("#bpErr");
    ok.hidden = true; err.hidden = true;
    try {
      await api.saveBrainProfile({
        company_name: panel.querySelector("#bpName").value,
        industry: panel.querySelector("#bpIndustry").value,
        business_desc: panel.querySelector("#bpDesc").value,
        fiscal_week_start: panel.querySelector("#bpWeek").value,
      });
      ok.hidden = false; ok.textContent = "已保存 — 之后的每份报告都会用上这些信息。";
    } catch (e) {
      err.hidden = false; err.textContent = e.message;
    }
  });
}

// ---------------------------------------------------------------------------
// ③b 指标定义 (KPI mappings — aliases is a free-text STRING, not a list)
// ---------------------------------------------------------------------------
async function renderKpiTab(panel) {
  const data = await api.getBrainProfile().then((d) => (d && d.profile) || {}).catch(() => ({}));
  if (!panel.isConnected) return;
  let rows = Array.isArray(data.kpi_mappings) ? data.kpi_mappings.slice() : [];

  function rowsHTML() {
    if (!rows.length) {
      return `<p class="muted" style="margin:8px 0;">还没有指标映射 — 添加后，AI 团队每次分析都会先按这份映射对齐你表格里的列名。</p>`;
    }
    return `<table class="ob-table"><thead><tr><th>指标</th><th>对应的列名（别名，逗号分隔）</th><th style="width:84px;">单位</th><th style="width:40px;"></th></tr></thead>
      <tbody>${rows.map((r, i) => `
        <tr>
          <td><input data-kpi="name" data-i="${i}" value="${esc(r.kpi_name_cn || "")}" placeholder="销售额"></td>
          <td><input data-kpi="aliases" data-i="${i}" value="${esc(r.aliases || "")}" placeholder="销售金额, 营收, revenue"></td>
          <td><input data-kpi="unit" data-i="${i}" value="${esc(r.unit || "")}" placeholder="元"></td>
          <td><button type="button" class="brain-del" data-rm="${i}" title="删除">✕</button></td>
        </tr>`).join("")}</tbody></table>`;
  }

  function paint() {
    panel.innerHTML = `
      <p class="muted" style="margin:4px 0 0;">告诉大脑你表格里的列名各对应哪个指标（口径只问一次，永久记住）。</p>
      <div id="kpiRows">${rowsHTML()}</div>
      <div class="brain-add-row">
        <button type="button" class="ghost-btn" id="kpiAdd">＋ 添加一行</button>
      </div>
      <p class="form-error" id="kpiErr" hidden></p>
      <p class="sch-ok" id="kpiOk" hidden></p>
      <button type="button" class="run-btn" id="kpiSave" style="max-width:220px;">保存指标定义</button>`;
    panel.querySelector("#kpiAdd").addEventListener("click", () => {
      collect();
      rows.push({ kpi_name_cn: "", aliases: "", unit: "" });
      paint();
    });
    panel.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", () => {
      collect();
      rows.splice(Number(b.dataset.rm), 1);
      paint();
    }));
    panel.querySelector("#kpiSave").addEventListener("click", save);
  }

  function collect() {
    panel.querySelectorAll("[data-kpi]").forEach((inp) => {
      const r = rows[Number(inp.dataset.i)];
      if (!r) return;
      if (inp.dataset.kpi === "name") r.kpi_name_cn = inp.value;
      if (inp.dataset.kpi === "aliases") r.aliases = inp.value;
      if (inp.dataset.kpi === "unit") r.unit = inp.value;
    });
  }

  async function save() {
    collect();
    const ok = panel.querySelector("#kpiOk");
    const err = panel.querySelector("#kpiErr");
    ok.hidden = true; err.hidden = true;
    const cleaned = rows.map((r) => ({
      kpi_name_cn: (r.kpi_name_cn || "").trim(),
      aliases: (r.aliases || "").trim(),
      unit: (r.unit || "").trim(),
    })).filter((r) => r.kpi_name_cn || r.aliases);
    try {
      await api.saveBrainProfile({ kpi_mappings: cleaned });
      rows = cleaned;
      paint();
      panel.querySelector("#kpiOk").hidden = false;
      panel.querySelector("#kpiOk").textContent = `已保存 ${cleaned.length} 条指标定义。`;
    } catch (e) {
      panel.querySelector("#kpiErr").hidden = false;
      panel.querySelector("#kpiErr").textContent = e.message;
    }
  }

  paint();
}

// ---------------------------------------------------------------------------
// ③c 术语表
// ---------------------------------------------------------------------------
async function renderGlossaryTab(panel, { onBrainChanged }) {
  let terms = [];
  try { terms = (await api.getBrainGlossary()).glossary || []; } catch (e) { /* empty */ }
  if (!panel.isConnected) return;

  function paint() {
    panel.innerHTML = `
      <p class="muted" style="margin:4px 0 0;">公司内部叫法（问一次，永久记住）— 报告里出现这些词时会按你的解释理解。</p>
      <div id="glRows">${terms.length ? terms.map((t) => `
        <div class="brain-row" data-id="${esc(t.id)}">
          <div class="brain-row-main">
            <div class="brain-row-term">${esc(t.term)}</div>
            <div class="brain-row-def">${esc(t.definition || "（未填写解释）")}</div>
          </div>
          <div class="brain-row-acts">
            <button type="button" class="brain-del" data-del="${esc(t.id)}">删除</button>
          </div>
        </div>`).join("") : `<p class="muted" style="margin:10px 0;">还没有术语。</p>`}</div>
      <div class="brain-add-row">
        <input id="glTerm" class="field-input" type="text" placeholder="术语，例如：大货">
        <input id="glDef" class="field-input" type="text" placeholder="它的含义，例如：批量采购订单" style="flex:2 1 220px;">
        <button type="button" class="ghost-btn" id="glAdd">添加</button>
      </div>
      <p class="form-error" id="glErr" hidden></p>`;
    panel.querySelector("#glAdd").addEventListener("click", async () => {
      const err = panel.querySelector("#glErr");
      err.hidden = true;
      const term = (panel.querySelector("#glTerm").value || "").trim();
      const def = (panel.querySelector("#glDef").value || "").trim();
      if (!term) { err.hidden = false; err.textContent = "请填写术语。"; return; }
      try {
        terms = (await api.addGlossaryTerm(term, def)).glossary || [];
        paint();
        onBrainChanged();
      } catch (e) { err.hidden = false; err.textContent = e.message; }
    });
    panel.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      try {
        terms = (await api.deleteGlossaryTerm(Number(b.dataset.del))).glossary || [];
        paint();
        onBrainChanged();
      } catch (e) {
        const err = panel.querySelector("#glErr");
        err.hidden = false; err.textContent = e.message;
      }
    }));
  }
  paint();
}

// ---------------------------------------------------------------------------
// ③d 报告偏好 (list + add + active toggle + delete)
// ---------------------------------------------------------------------------
async function renderPrefsTab(panel, { onBrainChanged }) {
  let prefs = [];
  try { prefs = (await api.getBrainPreferences()).preferences || []; } catch (e) { /* empty */ }
  if (!panel.isConnected) return;

  function paint() {
    panel.innerHTML = `
      <p class="muted" style="margin:4px 0 0;">长期偏好会作用于之后的每一份报告（在报告里点「存为长期偏好」也会出现在这里）。</p>
      <div id="pfRows">${prefs.length ? prefs.map((p) => `
        <div class="brain-row" data-id="${esc(p.id)}">
          <div class="brain-row-main">
            <div class="brain-row-text ${p.active ? "" : "off"}">${esc(p.instruction)}</div>
          </div>
          <div class="brain-row-acts">
            <label class="toggle" title="${p.active ? "生效中" : "已停用"}">
              <input type="checkbox" class="pf-toggle" data-id="${esc(p.id)}" ${p.active ? "checked" : ""}><span class="tknob"></span>
            </label>
            <button type="button" class="brain-del" data-del="${esc(p.id)}">删除</button>
          </div>
        </div>`).join("") : `<p class="muted" style="margin:10px 0;">还没有长期偏好。例如：「结论先行，正文不超过一页」。</p>`}</div>
      <div class="brain-add-row">
        <input id="pfText" class="field-input" type="text" placeholder="例如：每次报告先给三条核心结论" style="flex:3 1 260px;">
        <button type="button" class="ghost-btn" id="pfAdd">添加</button>
      </div>
      <p class="form-error" id="pfErr" hidden></p>`;
    const err = () => panel.querySelector("#pfErr");
    panel.querySelector("#pfAdd").addEventListener("click", async () => {
      err().hidden = true;
      const text = (panel.querySelector("#pfText").value || "").trim();
      if (!text) { err().hidden = false; err().textContent = "请填写偏好说明。"; return; }
      try {
        prefs = (await api.addBrainPreference(text)).preferences || [];
        paint();
        onBrainChanged();
      } catch (e) { err().hidden = false; err().textContent = e.message; }
    });
    panel.querySelectorAll(".pf-toggle").forEach((t) => t.addEventListener("change", async () => {
      try {
        prefs = (await api.toggleBrainPreference(Number(t.dataset.id), t.checked)).preferences || [];
        paint();
        onBrainChanged();
      } catch (e) {
        t.checked = !t.checked;
        err().hidden = false; err().textContent = e.message;
      }
    }));
    panel.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      try {
        prefs = (await api.deleteBrainPreference(Number(b.dataset.del))).preferences || [];
        paint();
        onBrainChanged();
      } catch (e) { err().hidden = false; err().textContent = e.message; }
    }));
  }
  paint();
}

// ---------------------------------------------------------------------------
// ③e 品牌 (logo + brand color; webhook stays in /schedule)
// ---------------------------------------------------------------------------
async function renderBrandTab(panel) {
  let branding = {};
  try { branding = (await api.getBrainBranding()).branding || {}; } catch (e) { /* defaults */ }
  if (!panel.isConnected) return;
  const color = /^#[0-9A-Fa-f]{6}$/.test(branding.brand_color || "") ? branding.brand_color : "#1D9E75";
  panel.innerHTML = `
    <p class="muted" style="margin:4px 0 0;">Logo 和品牌色会出现在每份报告和 PDF 上 — 转发出去的就是你公司的报告。</p>
    <label class="field-label">公司 Logo（PNG/JPG，≤ 1MB）</label>
    ${branding.logo_path ? `<img class="brand-logo-preview" id="brandLogoImg" src="/api/company-brain/logo?t=${Date.now()}" alt="公司 Logo">` : `<p class="muted" id="brandLogoNone" style="margin:4px 0;">还没有上传 Logo。</p>`}
    <input id="brandLogo" type="file" accept=".png,.jpg,.jpeg" class="ob-input">
    <label class="field-label" for="brandColor">品牌色</label>
    <div class="brand-color-row">
      <input id="brandColor" type="color" value="${esc(color)}">
      <code id="brandColorHex">${esc(color)}</code>
    </div>
    <p class="muted" style="margin:14px 0 0;">送达渠道（企业微信群机器人 Webhook）在
      <a href="/schedule" data-nav>「定时与送达」</a> 设置。</p>
    <p class="form-error" id="brandErr" hidden></p>
    <p class="sch-ok" id="brandOk" hidden></p>
    <button type="button" class="run-btn" id="brandSave" style="max-width:220px;">保存品牌设置</button>`;
  panel.querySelector("#brandColor").addEventListener("input", (e) => {
    panel.querySelector("#brandColorHex").textContent = e.target.value;
  });
  panel.querySelector("#brandSave").addEventListener("click", async () => {
    const ok = panel.querySelector("#brandOk");
    const err = panel.querySelector("#brandErr");
    ok.hidden = true; err.hidden = true;
    const file = panel.querySelector("#brandLogo").files[0];
    if (file && file.size > 1024 * 1024) {
      err.hidden = false; err.textContent = "Logo 文件不能超过 1MB。";
      return;
    }
    const fd = new FormData();
    fd.append("brand_color", panel.querySelector("#brandColor").value);
    if (file) fd.append("logo", file);
    try {
      await api.saveBrainBranding(fd);
      ok.hidden = false; ok.textContent = "已保存 — 下一份报告就会用上新的品牌样式。";
      if (file) renderBrandTab(panel); // re-render to show the fresh logo preview
    } catch (e) {
      err.hidden = false; err.textContent = e.message;
    }
  });
}

// ---------------------------------------------------------------------------
// ③f 历史数据导入 — the shared import flow (import_flow.js): stage → estimate
// → confirm → start → 900ms progress poll. Customer copy: 文件数 + 预计时间;
// ¥ renders ONLY when the API sent it (founder).
// ---------------------------------------------------------------------------
function renderImportTab(panel, { onBrainChanged }) {
  stopImportPoll();
  stopImport = mountImportFlow(panel, {
    lead: "导入过去 6–12 个月的表格，大脑立刻拥有历史 — 下一份报告就带趋势对比。",
    onFinished: onBrainChanged,   // metrics_count grew — refresh the hero stats
  });
}
