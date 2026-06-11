// ===========================================================================
// founder/team.js — 团队与模型 (the old “Your Team” page, ported as-is, dark
// scope). Per-agent model dropdowns; changes apply to the next run. The CFO
// budget meters moved to their own 成本控制台 view (founder/costs.js).
// ===========================================================================
import * as api from "../api.js";
import { state, loadFounderCatalog } from "../state.js";
import { esc } from "../components.js";
import { founderFrame, requireAdmin } from "./common.js";

export async function mount(el) {
  if (!requireAdmin(el)) return;
  el.innerHTML = founderFrame(`<p class="muted">加载团队…</p>`);
  await loadFounderCatalog();
  render(el);
}

function whoName(id) {
  const a = state.agents.find((x) => x.id === id);
  return a ? `${a.name} · ${a.title}` : id;
}

function render(el) {
  el.innerHTML = founderFrame(bodyHTML());
  el.querySelectorAll(".model-select").forEach((sel) =>
    sel.addEventListener("change", () => setAgentModel(el, sel.dataset.agent, sel.value, sel))
  );
}

function bodyHTML() {
  if (!state.agents.length) {
    return `<h2 class="detail-h">团队与模型</h2><p class="muted">团队尚未加载。</p>`;
  }
  const ceo = state.agents.find((a) => a.reports_to === null);
  const workers = state.agents.filter((a) => a.reports_to !== null);
  const modelOptions = (selectedId) =>
    state.models.map((m) =>
      `<option value="${esc(m.id)}" ${m.id === selectedId ? "selected" : ""}>${esc(m.label)}</option>`).join("");
  const card = (a) => {
    const isCeo = !!(ceo && a.id === ceo.id);
    const modelControl = state.models.length
      ? `<select class="model-select" data-agent="${esc(a.id)}" aria-label="Model for ${esc(a.name)}">${modelOptions(a.model)}</select>`
      : `<span class="chip model">${esc(a.model || "—")}</span>`;
    return `<div class="agent-card ${isCeo ? "ceo" : ""}">
      <div class="agent-card-top">
        <span class="node-dot"></span>
        <span class="agent-name">${esc(a.name)}${a.name_cn ? ` · ${esc(a.name_cn)}` : ""}</span>
      </div>
      <div class="agent-role">${esc(a.title)}</div>
      <div class="agent-meta">${modelControl}</div>
      <div class="agent-reports">${isCeo ? "Leads the team" : `Reports to ${esc(whoName(a.reports_to))}`}</div>
    </div>`;
  };
  return `
    <h2 class="detail-h">团队与模型</h2>
    <p class="detail-lead">${state.agents.length} 位智能体按公司结构组织。在卡片里为每位成员挑选模型 — 下一次运行即生效。客户界面只会看到中文角色名，永远看不到这里的模型机制。</p>
    ${ceo ? `<div class="detail-section"><div class="block-title">Leadership</div><div class="agent-grid agent-grid-ceo">${card(ceo)}</div></div>` : ""}
    <div class="detail-section">
      <div class="block-title">Specialists</div>
      <div class="agent-grid">${workers.map(card).join("")}</div>
    </div>`;
}

async function setAgentModel(el, agentId, model, selEl) {
  const prev = state.agents.find((a) => a.id === agentId);
  const prevModel = prev ? prev.model : null;
  if (selEl) selEl.disabled = true;
  try {
    state.agents = await api.setAgentModel(agentId, model);
    render(el);
  } catch (e) {
    if (selEl && prevModel) selEl.value = prevModel; // revert the dropdown on failure
    alert(e.message);
  } finally {
    if (selEl) selEl.disabled = false;
  }
}
