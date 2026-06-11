// ===========================================================================
// schedule.js — 定时与送达 (plan §3.6, B5). 企业版 accounts get the editors:
// ① 定时任务 — preset 每周一 08:00, frequency picker 每周/每月/高级(cron),
//   report type from the goal templates, data source, active toggle, and a
//   founder-only per-run budget field;
// ② 送达渠道 — the account email plus the 企业微信群机器人 webhook (saved as a
//   branding field) with 发送测试消息 per channel;
// ③ 数据收件箱 — the staged files the next "最新上传" fire will use, with
//   drag-drop add and per-file remove (no run is started).
// Non-企业版 accounts see the Monday-ritual preview + upgrade CTA instead (D3);
// the backend enforces the same gate with a structured 402.
// ===========================================================================
import * as api from "../api.js";
import { state, isAdmin } from "../state.js";
import { esc, fmtBytes, pageHead } from "../components.js";

// Weekdays are composed as NAMES (mon…sun), never numbers: APScheduler's
// from_crontab numbers day_of_week 0=Monday (not the crontab-standard
// 0=Sunday), so numeric weekdays mean different days to different readers.
// Names mean the same day under both conventions.
const WEEKDAYS = [["mon", "周一"], ["tue", "周二"], ["wed", "周三"], ["thu", "周四"],
                  ["fri", "周五"], ["sat", "周六"], ["sun", "周日"]];
const WEEKDAY_CN = { mon: "周一", tue: "周二", wed: "周三", thu: "周四",
                     fri: "周五", sat: "周六", sun: "周日" };
// Numeric weekdays in legacy rows are shown per the ENGINE that fires them
// (APScheduler: 0=Monday … 6=Sunday) — the label must tell the truth.
const WEEKDAY_NUM_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

// "0 8 * * mon" → 每周一 08:00; "0 9 1 * *" → 每月1日 09:00; fancier → raw.
export function cronLabel(expr) {
  const m = /^(\d{1,2})\s+(\d{1,2})\s+(\S+)\s+(\S+)\s+(\S+)$/.exec((expr || "").trim());
  if (m) {
    const time = `${String(+m[2]).padStart(2, "0")}:${String(+m[1]).padStart(2, "0")}`;
    const dow = m[5].toLowerCase();
    if (m[3] === "*" && m[4] === "*" && WEEKDAY_CN[dow]) return `每${WEEKDAY_CN[dow]} ${time}`;
    if (m[3] === "*" && m[4] === "*" && /^[0-6]$/.test(dow)) return `每${WEEKDAY_NUM_CN[+dow]} ${time}`;
    if (/^\d{1,2}$/.test(m[3]) && m[4] === "*" && m[5] === "*") return `每月${+m[3]}日 ${time}`;
    if (m[3] === "*" && m[4] === "*" && m[5] === "*") return `每天 ${time}`;
  }
  return `自定义（${expr}）`;
}

// next_run_at arrives as Asia/Shanghai wall-clock ISO ("…T08:00:00+08:00") —
// the clock the customer set ("每周一 08:00"). Format from the string, not via
// Date, so a browser in another timezone doesn't silently shift the label.
export function fmtNextRun(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || "");
  if (!m) return "";
  return `${+m[2]}月${+m[3]}日 ${m[4]}:${m[5]}`;
}

const goalLabel = (goal) => {
  const t = ((state.meta && state.meta.report_goal_templates) || []).find((x) => x.goal === goal);
  return t ? t.label : "经营周报";
};

const channelLabel = (c) => ({ email: "邮箱", wechat: "企业微信", service_account: "服务号" }[c] || c);

// ---------------------------------------------------------------------------
export async function mount(el, ctx) {
  el.innerHTML = `<div class="page page-narrow">
    ${pageHead("定时与送达", "设定「每周一 08:00」，报告自动生成并送到企业微信和邮箱 — 你什么都不用做。")}
    <div id="schBody"><div class="card"><p class="muted">正在载入…</p></div></div>
  </div>`;
  const body = el.querySelector("#schBody");

  let plansData = null;
  try { plansData = await api.getPlans(); } catch (e) { /* render as gated below */ }
  if (!body.isConnected) return;
  const cur = (plansData && plansData.current) || {};
  // `current` carries only plan_code — join it against plans[] for the features.
  const planRow = ((plansData && plansData.plans) || []).find((p) => p.code === cur.plan_code) || {};
  const canSchedule = ((planRow.features) || []).includes("scheduled_delivery");

  if (!canSchedule) {
    renderUpsell(body, ctx, plansData);
    return;
  }
  return renderEditors(body, ctx);
}

// ---------------------------------------------------------------------------
// D3 — non-企业版: the Monday-ritual preview + upgrade CTA, no editors.
// ---------------------------------------------------------------------------
function renderUpsell(body, ctx, plansData) {
  const pro = ((plansData && plansData.plans) || []).find((p) => p.code === "pro") || {};
  const proLabel = pro.label || "企业版";
  const price = pro.monthly_price_yuan ? `¥${Math.round(pro.monthly_price_yuan)}/月` : "";
  body.innerHTML = `<div class="card">
      <h3 class="card-title">周一早上，报告自己到 — ${esc(proLabel)}专属</h3>
      <p class="muted" style="margin:6px 0 0;">升级到${esc(proLabel)}${price ? `（${esc(price)}）` : ""}后，这一页变成你的自动驾驶舱：</p>
      <div class="ritual-preview" aria-hidden="true">
        <div class="ritual-line"><span class="ritual-ico">⏰</span><span><strong>每周一 08:00</strong> 自动用你最近上传的数据生成经营周报</span></div>
        <div class="ritual-line"><span class="ritual-ico">📱</span><span>核心结论推送到<strong>企业微信群</strong>，PDF 同步发到<strong>邮箱</strong></span></div>
        <div class="ritual-line"><span class="ritual-ico">📂</span><span>平时把新数据丢进<strong>数据收件箱</strong>即可，其余全自动</span></div>
        <div class="ritual-line"><span class="ritual-ico">🧪</span><span>一键<strong>发送测试消息</strong>，确认渠道畅通</span></div>
      </div>
      <button type="button" class="run-btn" id="schUpgrade" style="max-width:320px;">升级到${esc(proLabel)}，开启每周自动送达</button>
      <p class="muted" style="margin:10px 0 0;font-size:12.5px;">现在仍可随时在「生成报告」页手动生成报告。</p>
    </div>`;
  body.querySelector("#schUpgrade").addEventListener("click", () => ctx.navigate("/account"));
}

// ---------------------------------------------------------------------------
// 企业版 — the three editors.
// ---------------------------------------------------------------------------
async function renderEditors(body, ctx) {
  const templates = (state.meta && state.meta.report_goal_templates) || [];
  const account = state.account || {};

  body.innerHTML = `
    <div class="card" id="schJobsCard">
      <h3 class="card-title">⏰ 定时任务</h3>
      <div id="schJobs"><p class="muted">正在载入…</p></div>
      <h3 class="card-title" style="margin-top:20px;">新建定时任务</h3>
      <label class="field-label" for="schName">任务名称</label>
      <input id="schName" class="field-input" type="text" value="每周经营周报">
      <label class="field-label" for="schType">报告类型</label>
      <select id="schType" class="ob-select">
        ${templates.map((t, i) => `<option value="${esc(t.code)}" ${i === 0 ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
      </select>
      <label class="field-label">生成频率</label>
      <div class="freq-tabs" id="schFreqTabs">
        <button type="button" class="freq-tab on" data-freq="weekly">每周</button>
        <button type="button" class="freq-tab" data-freq="monthly">每月</button>
        <button type="button" class="freq-tab" data-freq="cron">高级</button>
      </div>
      <div class="sch-grid" id="schFreqWeekly">
        <select id="schWeekday" class="ob-select">
          ${WEEKDAYS.map(([v, l]) => `<option value="${v}" ${v === "mon" ? "selected" : ""}>${l}</option>`).join("")}
        </select>
        <input id="schTimeW" class="field-input" type="time" value="08:00">
      </div>
      <div class="sch-grid" id="schFreqMonthly" hidden>
        <span class="sch-inline-label">每月</span>
        <select id="schMonthDay" class="ob-select">
          ${Array.from({ length: 28 }, (_, i) => `<option value="${i + 1}">${i + 1} 日</option>`).join("")}
        </select>
        <input id="schTimeM" class="field-input" type="time" value="08:00">
      </div>
      <div class="sch-grid" id="schFreqCron" hidden>
        <input id="schCron" class="field-input" type="text" placeholder="cron 表达式，例如 0 8 * * mon" style="min-width:240px;">
      </div>
      <label class="field-label" for="schSource">数据来源</label>
      <select id="schSource" class="ob-select">
        <option value="latest_upload" selected>最新上传（数据收件箱里的文件）</option>
        <option value="fixed_files">固定文件（指定路径）</option>
      </select>
      <div id="schFixedWrap" hidden>
        <label class="field-label" for="schFixed">固定文件路径（每行一个，相对项目目录）</label>
        <textarea id="schFixed" class="field-input" rows="2" placeholder="assets/示例销售数据.csv"></textarea>
      </div>
      <label class="field-label">送达渠道</label>
      <div class="sch-check-row">
        <label class="ob-check"><input type="checkbox" id="schChEmail" checked> 邮箱</label>
        <label class="ob-check"><input type="checkbox" id="schChWechat"> 企业微信群机器人</label>
      </div>
      ${isAdmin() ? `
      <label class="field-label" for="schBudget">单次预算上限（¥，仅内部可见，留空不限）</label>
      <input id="schBudget" class="field-input" type="number" min="0" step="1" placeholder="不限">` : ""}
      <div class="sch-check-row" style="align-items:center;margin-top:14px;">
        <label class="toggle"><input type="checkbox" id="schActive" checked><span class="tknob"></span></label>
        <span class="sch-inline-label">立即启用</span>
      </div>
      <p class="form-error" id="schErr" hidden></p>
      <p class="sch-ok" id="schOk" hidden></p>
      <button type="button" class="run-btn" id="schSave">保存定时任务</button>
    </div>

    <div class="card">
      <h3 class="card-title">📮 送达渠道</h3>
      <div class="chan-row">
        <div class="chan-head">
          <span class="chan-name">邮箱</span>
          <span class="chan-value">${esc(account.email || "")}（账号邮箱）</span>
          <button type="button" class="chan-test" data-channel="email">发送测试消息</button>
        </div>
        <div class="chan-result" id="chanResEmail" hidden></div>
      </div>
      <div class="chan-row">
        <div class="chan-head">
          <span class="chan-name">企业微信群机器人</span>
          <button type="button" class="chan-test" data-channel="wechat">发送测试消息</button>
        </div>
        <div class="chan-save-row">
          <input id="schWebhook" class="field-input" type="text" placeholder="粘贴群机器人的 Webhook 地址">
          <button type="button" class="ghost-btn" id="schWebhookSave">保存</button>
        </div>
        <div class="chan-result" id="chanResWechat" hidden></div>
        <ol class="chan-howto">
          <li>在企业微信里打开要接收报告的群，点右上角「···」→「添加群机器人」</li>
          <li>新建机器人（名字随意，例如「经营周报」），创建后复制它的 Webhook 地址</li>
          <li>粘贴到上面并保存，点「发送测试消息」确认群里能收到</li>
        </ol>
      </div>
    </div>

    <div class="card">
      <h3 class="card-title">📂 数据收件箱</h3>
      <p class="muted" style="margin:4px 0 0;">下次报告将使用这些文件。</p>
      <ul class="file-list" id="inboxFiles"></ul>
      <div class="drop-zone" id="inboxDrop" tabindex="0" style="margin-top:12px;">
        <div class="dz-icon">📥</div>
        <div class="dz-title">拖拽文件到这里，或点击添加</div>
        <div class="dz-sub">Excel（.xlsx）或 CSV，单文件 ≤ 25MB</div>
        <input id="inboxFile" type="file" multiple accept=".csv,.xlsx,.xls" hidden>
      </div>
      <p class="form-error" id="inboxErr" hidden></p>
      <p class="inbox-note">提示：在「生成报告」页上传文件会<strong>替换</strong>这里的全部文件；定时任务触发时若收件箱为空，会在没有数据文件的情况下生成报告。</p>
    </div>`;

  const $ = (id) => body.querySelector("#" + id);

  // ---- ① schedule card ------------------------------------------------------
  let freq = "weekly";
  $("schFreqTabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".freq-tab");
    if (!tab) return;
    freq = tab.dataset.freq;
    body.querySelectorAll(".freq-tab").forEach((t) => t.classList.toggle("on", t === tab));
    $("schFreqWeekly").hidden = freq !== "weekly";
    $("schFreqMonthly").hidden = freq !== "monthly";
    $("schFreqCron").hidden = freq !== "cron";
  });
  $("schSource").addEventListener("change", () => {
    $("schFixedWrap").hidden = $("schSource").value !== "fixed_files";
  });

  function composeCron() {
    if (freq === "weekly") {
      const [h, m] = ($("schTimeW").value || "08:00").split(":");
      return `${+m} ${+h} * * ${$("schWeekday").value}`;
    }
    if (freq === "monthly") {
      const [h, m] = ($("schTimeM").value || "08:00").split(":");
      return `${+m} ${+h} ${$("schMonthDay").value} * *`;
    }
    return ($("schCron").value || "").trim();
  }

  function jobRowHTML(j) {
    const chans = (j.delivery_channels || []).map(channelLabel).join(" + ") || "未设置渠道";
    const next = j.next_run_at ? `<span class="sch-job-next">下次：${esc(fmtNextRun(j.next_run_at))}</span>` : `<span class="muted">已暂停</span>`;
    return `<div class="sch-job" data-id="${esc(j.id)}">
        <div class="sch-job-main">
          <div class="sch-job-name">${esc(j.name_cn)}</div>
          <div class="sch-job-sub">${esc(cronLabel(j.cron_expr))} · ${esc(goalLabel(j.goal_template))} · ${esc(chans)} · ${next}</div>
        </div>
        <div class="sch-job-acts">
          <label class="toggle" title="${j.active ? "已启用" : "已暂停"}">
            <input type="checkbox" class="sch-toggle" ${j.active ? "checked" : ""}><span class="tknob"></span>
          </label>
          <button type="button" class="sch-del">删除</button>
        </div>
      </div>`;
  }

  function renderJobs(jobs) {
    $("schJobs").innerHTML = jobs.length
      ? jobs.map(jobRowHTML).join("")
      : `<p class="muted" style="margin:6px 0 0;">还没有定时任务 — 用下面的表单创建一个，默认就是「每周一 08:00」。</p>`;
  }

  let jobs = [];
  try {
    jobs = (await api.getScheduledJobs()).jobs || [];
  } catch (e) { /* card shows the empty state */ }
  if (!body.isConnected) return;
  renderJobs(jobs);

  $("schJobs").addEventListener("click", async (e) => {
    const row = e.target.closest(".sch-job");
    if (!row) return;
    const id = Number(row.dataset.id);
    if (e.target.closest(".sch-del")) {
      if (!window.confirm("确定删除这个定时任务？")) return;
      try {
        jobs = (await api.deleteScheduledJob(id)).jobs || [];
        renderJobs(jobs);
      } catch (err) { showErr(err.message); }
      return;
    }
    const tog = e.target.closest(".sch-toggle");
    if (tog) {
      try {
        jobs = (await api.updateScheduledJob({ id, active: tog.checked })).jobs || [];
        renderJobs(jobs);
      } catch (err) {
        tog.checked = !tog.checked;
        showErr(err.message);
      }
    }
  });

  function showErr(msg) {
    $("schOk").hidden = true;
    const p = $("schErr");
    p.hidden = false;
    p.textContent = msg || "操作失败，请稍后重试。";
  }

  $("schSave").addEventListener("click", async () => {
    $("schErr").hidden = true; $("schOk").hidden = true;
    const cron = composeCron();
    if (!cron) { showErr("请填写生成频率。"); return; }
    const t = templates.find((x) => x.code === $("schType").value) || templates[0] || {};
    const channels = [];
    if ($("schChEmail").checked) channels.push("email");
    if ($("schChWechat").checked) channels.push("wechat");
    const payload = {
      name_cn: ($("schName").value || "").trim() || "每周经营周报",
      cron_expr: cron,
      goal_template: t.goal || "生成上周经营周报",
      source_mode: $("schSource").value,
      fixed_file_paths: $("schSource").value === "fixed_files"
        ? ($("schFixed").value || "").split("\n").map((s) => s.trim()).filter(Boolean) : [],
      delivery_channels: channels,
      active: $("schActive").checked,
    };
    const budgetEl = $("schBudget");
    if (budgetEl && budgetEl.value) payload.budget_cny = Number(budgetEl.value);
    const btn = $("schSave");
    btn.disabled = true; btn.textContent = "保存中…";
    try {
      const data = await api.createScheduledJob(payload);
      jobs = data.jobs || [];
      renderJobs(jobs);
      const created = jobs.find((j) => j.id === data.job_id);
      $("schOk").hidden = false;
      $("schOk").textContent = `已创建「${payload.name_cn}」${created && created.next_run_at ? ` — 下次运行：${fmtNextRun(created.next_run_at)}` : ""}`;
    } catch (err) {
      showErr(err.message);
    }
    btn.disabled = false; btn.textContent = "保存定时任务";
  });

  // ---- ② 送达渠道 -------------------------------------------------------------
  try {
    const branding = (await api.getBrainBranding()).branding || {};
    if (body.isConnected) {
      $("schWebhook").value = branding.wechat_webhook || "";
      $("schChWechat").checked = !!branding.wechat_webhook;
    }
  } catch (e) { /* the field just starts empty */ }

  $("schWebhookSave").addEventListener("click", async () => {
    const btn = $("schWebhookSave");
    const res = $("chanResWechat");
    btn.disabled = true; btn.textContent = "保存中…";
    try {
      const fd = new FormData();
      fd.append("wechat_webhook", ($("schWebhook").value || "").trim());
      await api.saveBrainBranding(fd);
      res.hidden = false; res.className = "chan-result ok";
      res.textContent = "已保存。点「发送测试消息」确认群里能收到。";
    } catch (err) {
      res.hidden = false; res.className = "chan-result err";
      res.textContent = err.message;
    }
    btn.disabled = false; btn.textContent = "保存";
  });

  body.querySelectorAll(".chan-test").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const channel = btn.dataset.channel;
      const res = $(channel === "email" ? "chanResEmail" : "chanResWechat");
      btn.disabled = true; btn.textContent = "发送中…";
      try {
        const data = await api.deliveryTest(channel);
        res.hidden = false;
        if (data && data.ok) {
          res.className = "chan-result ok";
          res.textContent = channel === "email" ? "测试邮件已发出，请检查收件箱。" : "测试消息已发出，请查看企业微信群。";
        } else {
          res.className = "chan-result err";
          res.textContent = (data && data.error) || "发送失败，请稍后重试。";
        }
      } catch (err) {
        res.hidden = false; res.className = "chan-result err";
        res.textContent = err.message;
      }
      btn.disabled = false; btn.textContent = "发送测试消息";
    });
  });

  // ---- ③ 数据收件箱 ------------------------------------------------------------
  function renderInbox(data) {
    const files = (data && data.files) || [];
    $("inboxFiles").innerHTML = files.length
      ? files.map((f) => `
        <li class="file-item">
          <span class="file-name">${esc(f.name)}</span>
          <span class="file-size">${esc(fmtBytes(f.size || 0))}</span>
          <button type="button" class="file-rm" data-name="${esc(f.name)}" title="移除" aria-label="移除 ${esc(f.name)}">&times;</button>
        </li>`).join("")
      : `<li class="muted" style="list-style:none;margin-top:8px;">收件箱是空的 — 拖入最近的经营数据，下次定时报告就会用上。</li>`;
  }

  function showInboxErr(msg) {
    const p = $("inboxErr");
    p.hidden = !msg;
    p.textContent = msg || "";
  }

  try { renderInbox(await api.getUploadsLatest()); } catch (e) { /* empty state */ }
  if (!body.isConnected) return;

  async function addInboxFiles(fileList) {
    const files = Array.from(fileList || []).filter((f) => f && f.name);
    if (!files.length) return;
    showInboxErr("");
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    try { renderInbox(await api.modifyUploads(fd)); } catch (err) { showInboxErr(err.message); }
  }

  const drop = $("inboxDrop");
  drop.addEventListener("click", () => $("inboxFile").click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("inboxFile").click(); }
  });
  $("inboxFile").addEventListener("change", (e) => { addInboxFiles(e.target.files); e.target.value = ""; });
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("drag");
  }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove("drag");
  }));
  drop.addEventListener("drop", (e) => addInboxFiles(e.dataTransfer && e.dataTransfer.files));
  $("inboxFiles").addEventListener("click", async (e) => {
    const btn = e.target.closest(".file-rm");
    if (!btn) return;
    showInboxErr("");
    try { renderInbox(await api.removeUploads([btn.dataset.name])); } catch (err) { showInboxErr(err.message); }
  });
}
