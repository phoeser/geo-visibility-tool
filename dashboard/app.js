/* ----------------------------------------------------------------------
 * GEO Visibility Dashboard
 *
 * Tabs: Dashboard | Historie | Config
 *
 * Daten werden aus dem GitHub-Repo via GitHub Pages relativ geladen.
 * Config-Änderungen werden via GitHub API direkt ins Repo gepusht.
 * -------------------------------------------------------------------- */

const BRAND_COLOR = "#e11d48";
const COMP_COLORS = ["#3b82f6", "#8b5cf6", "#22c55e", "#f59e0b", "#06b6d4", "#ec4899", "#f97316", "#14b8a6", "#a855f7", "#eab308"];

const state = {
  runs: [],
  currentRun: null,
  selectedRunFile: null,
  selectedProduct: "all",
  selectedLLM: "all",
  charts: {},
  basePath: "",
  historyRuns: [],   // voll geladene Run-JSONs fuer Trend-Charts
  config: null,      // aktuell geladene config.json
  prompts: {},       // { product_id: {product, description, prompts: [...] } }
  configLoaded: false,
};

// ----------------------------------------------------------------------
// Data Loading (bestehend)
// ----------------------------------------------------------------------

async function tryFetch(paths) {
  for (const p of paths) {
    try {
      const r = await fetch(p, { cache: "no-cache" });
      if (r.ok) return { data: await r.json(), path: p };
    } catch (e) {}
  }
  return null;
}

async function loadIndex() {
  const candidates = ["../data/runs/index.json", "data/runs/index.json"];
  const res = await tryFetch(candidates);
  return res ? { runs: res.data.runs || [], basePath: res.path.replace("index.json", "") } : null;
}

async function loadRun(file, basePath) {
  const res = await tryFetch([basePath + file]);
  return res ? res.data : null;
}

// ----------------------------------------------------------------------
// Format-Helpers
// ----------------------------------------------------------------------

function fmtPct(v) { if (v === null || v === undefined) return "–"; return (v * 100).toFixed(1) + " %"; }
function fmtNum(v, d = 2) { if (v === null || v === undefined) return "–"; return Number(v).toFixed(d); }
function fmtDelta(v, isPct = true) {
  if (v === null || v === undefined) return { text: "–", cls: "flat" };
  const pretty = isPct ? (v * 100).toFixed(1) + " %-Pt" : v.toFixed(2);
  if (v > 0.0005) return { text: "▲ " + pretty, cls: "up" };
  if (v < -0.0005) return { text: "▼ " + pretty.replace("-", ""), cls: "down" };
  return { text: "– " + pretty, cls: "flat" };
}
function destroyChart(key) { if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; } }
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (m) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
  ));
}
function $(id) { return document.getElementById(id); }

// ----------------------------------------------------------------------
// Aggregation (fuer Dashboard-Tab)
// ----------------------------------------------------------------------

function aggregate(runOverride, productFilter, llmFilter) {
  const run = runOverride || state.currentRun;
  if (!run) return null;

  const productIds = (productFilter || state.selectedProduct) === "all"
    ? Object.keys(run.products) : [(productFilter || state.selectedProduct)];
  const llms = (llmFilter || state.selectedLLM) === "all" ? run.llms : [(llmFilter || state.selectedLLM)];
  const brandOrder = [run.brand, ...(run.competitors || [])];

  const totals = {};
  brandOrder.forEach(n => totals[n] = { mentions: 0, appearances: 0, prompts: 0, citations: 0, ranks: [] });

  productIds.forEach(pid => {
    const p = run.products[pid];
    if (!p) return;
    llms.forEach(llm => {
      const sum = p.summary_by_llm && p.summary_by_llm[llm];
      if (!sum) return;
      sum.brands.forEach(b => {
        if (!totals[b.name]) return;
        totals[b.name].prompts += sum.prompts_total;
        totals[b.name].mentions += b.mentions;
        totals[b.name].appearances += Math.round(b.appearance_rate * sum.prompts_total);
        totals[b.name].citations += Math.round(b.citation_rate * sum.prompts_total);
        if (b.avg_rank !== null && b.avg_rank !== undefined) totals[b.name].ranks.push(b.avg_rank);
      });
    });
  });

  const grandMentions = Object.values(totals).reduce((a, b) => a + b.mentions, 0) || 1;
  return brandOrder.map(n => {
    const d = totals[n];
    return {
      name: n,
      mentions: d.mentions,
      share_of_voice: d.mentions / grandMentions,
      appearance_rate: d.prompts ? d.appearances / d.prompts : 0,
      citation_rate: d.prompts ? d.citations / d.prompts : 0,
      avg_rank: d.ranks.length ? d.ranks.reduce((a, b) => a + b, 0) / d.ranks.length : null,
    };
  });
}

// ----------------------------------------------------------------------
// Dashboard-Tab Rendering (bestehend, leicht angepasst)
// ----------------------------------------------------------------------

function renderRunMeta() {
  const run = state.currentRun;
  if (!run) { $("runMeta").textContent = "Keine Daten"; return; }
  const when = run.finished_at ? new Date(run.finished_at).toLocaleString("de-DE") : "?";
  $("runMeta").innerHTML = `<strong>${run.brand}</strong> — Lauf ${run.run_id} • ${when} • LLMs: ${run.llms.join(", ")}`;
}

function renderControls() {
  const run = state.currentRun;
  const prod = $("productSelector");
  prod.innerHTML = '<option value="all">Alle Produkte</option>';
  Object.entries(run.products).forEach(([id, p]) => {
    prod.insertAdjacentHTML("beforeend", `<option value="${id}">${p.name}</option>`);
  });
  prod.value = state.selectedProduct;

  const llm = $("llmSelector");
  llm.innerHTML = '<option value="all">Alle LLMs</option>';
  run.llms.forEach(id => llm.insertAdjacentHTML("beforeend", `<option value="${id}">${id}</option>`));
  llm.value = state.selectedLLM;

  const runs = $("runSelector");
  runs.innerHTML = "";
  state.runs.slice().reverse().forEach(r => {
    const opt = document.createElement("option");
    opt.value = r.file;
    opt.textContent = r.run_id;
    if (r.file === state.selectedRunFile) opt.selected = true;
    runs.appendChild(opt);
  });
}

function renderKPIs() {
  const agg = aggregate(); const run = state.currentRun;
  const brand = run.brand;
  const brandRow = agg.find(a => a.name === brand);
  if (!brandRow) return;
  const ranked = agg.slice().sort((a, b) => b.share_of_voice - a.share_of_voice);
  const brandPos = ranked.findIndex(r => r.name === brand) + 1;
  const deltas = (run.impact && run.impact.deltas && run.impact.deltas.changes) || [];
  const brandDeltas = deltas.filter(d => d.brand === brand &&
    (state.selectedProduct === "all" || d.product === state.selectedProduct) &&
    (state.selectedLLM === "all" || d.llm === state.selectedLLM));
  const avg = (k) => brandDeltas.length ? brandDeltas.reduce((a, b) => a + (b[k] || 0), 0) / brandDeltas.length : null;

  const kpis = [
    { label: "Share of Voice", value: fmtPct(brandRow.share_of_voice), delta: fmtDelta(avg("delta_share_of_voice")) },
    { label: "Nennungs-Quote", value: fmtPct(brandRow.appearance_rate), delta: fmtDelta(avg("delta_appearance_rate")) },
    { label: "Zitierungs-Quote", value: fmtPct(brandRow.citation_rate), delta: fmtDelta(avg("delta_citation_rate")) },
    { label: "Ø Rang in Listen", value: fmtNum(brandRow.avg_rank, 2),
      delta: fmtDelta(avg("delta_avg_rank") ? -avg("delta_avg_rank") : null, false) },
    { label: "Position im Markt", value: brandPos + " / " + agg.length,
      delta: { text: "unter " + agg.length + " Marken", cls: "flat" } },
  ];
  $("kpiRow").innerHTML = kpis.map(k => `
    <div class="kpi">
      <div class="label">${k.label}</div>
      <div class="value">${k.value}</div>
      <div class="delta ${k.delta.cls}">${k.delta.text}</div>
    </div>`).join("");
}

function renderExecSummary() {
  const run = state.currentRun;
  const agg = aggregate();
  if (!agg || !agg.length) {
    $("execSummary").textContent = "Noch keine Daten.";
    return;
  }
  const brand = run.brand;
  const brandRow = agg.find(a => a.name === brand);
  const ranked = agg.slice().sort((a, b) => b.share_of_voice - a.share_of_voice);
  const pos = ranked.findIndex(r => r.name === brand) + 1;
  const top3 = ranked.slice(0, 3).map(r => `${r.name} ${fmtPct(r.share_of_voice)}`).join(", ");

  // Scope-Label: was ist gefiltert?
  let scope = "Gesamt";
  if (state.selectedProduct !== "all") {
    const p = run.products[state.selectedProduct];
    scope = p ? p.name : state.selectedProduct;
  }
  if (state.selectedLLM !== "all") scope += ` · ${state.selectedLLM}`;

  const lines = [];
  lines.push(`<strong>${escapeHtml(scope)}</strong> — ${brand} auf Platz ${pos}/${agg.length}.`);
  if (brandRow) {
    lines.push(`SoV ${fmtPct(brandRow.share_of_voice)} · Nennungs-Quote ${fmtPct(brandRow.appearance_rate)} · Zitierung ${fmtPct(brandRow.citation_rate)}` +
      (brandRow.avg_rank != null ? ` · Ø Rang ${fmtNum(brandRow.avg_rank, 1)}` : ""));
  }
  lines.push(`<span class="hint">Top 3: ${top3}</span>`);
  $("execSummary").innerHTML = lines.join("<br/>");
}

function makeBarChart(canvasId, key, labels, values, horizontal) {
  destroyChart(key);
  const colors = labels.map((n, i) => n === state.currentRun.brand ? BRAND_COLOR : COMP_COLORS[i % COMP_COLORS.length]);
  state.charts[key] = new Chart($(canvasId), {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: colors, borderRadius: 6 }] },
    options: {
      indexAxis: horizontal ? "y" : "x",
      plugins: { legend: { display: false } },
      scales: {
        x: { beginAtZero: true, ticks: { color: "#8b949e" }, grid: { color: "rgba(255,255,255,0.05)" } },
        y: { beginAtZero: true, ticks: { color: "#e6edf3" }, grid: { display: false } },
      },
    },
  });
}

function renderSovChart() {
  const agg = aggregate();
  makeBarChart("sovChart", "sov",
    agg.map(a => a.name),
    agg.map(a => Math.round(a.share_of_voice * 10000) / 100), false);
}
function renderAppearanceChart() {
  const agg = aggregate();
  makeBarChart("appearanceChart", "app",
    agg.map(a => a.name),
    agg.map(a => Math.round(a.appearance_rate * 10000) / 100), true);
}
function renderRankChart() {
  const agg = aggregate();
  makeBarChart("rankChart", "rank",
    agg.map(a => a.name),
    agg.map(a => a.avg_rank), true);
}
function renderCitationChart() {
  const agg = aggregate();
  makeBarChart("citationChart", "cit",
    agg.map(a => a.name),
    agg.map(a => Math.round(a.citation_rate * 10000) / 100), true);
}

function renderDeltasTable() {
  const run = state.currentRun;
  const deltas = (run.impact && run.impact.deltas && run.impact.deltas.changes) || [];
  const filtered = deltas.filter(d =>
    (state.selectedProduct === "all" || d.product === state.selectedProduct) &&
    (state.selectedLLM === "all" || d.llm === state.selectedLLM));
  const sorted = filtered.slice().sort((a, b) =>
    Math.abs(b.delta_share_of_voice || 0) - Math.abs(a.delta_share_of_voice || 0)).slice(0, 10);
  const c = $("deltasTable");
  if (!sorted.length) { c.innerHTML = `<p class="hint">Keine vergleichbaren Daten — vermutlich erster Lauf.</p>`; return; }
  c.innerHTML = `<table><thead><tr>
    <th>Marke</th><th>Produkt</th><th>LLM</th>
    <th>ΔShare of Voice</th><th>ΔNennungs-Quote</th><th>ΔZitierung</th><th>ΔØ Rang</th>
    </tr></thead><tbody>${sorted.map(d => {
      const sov = fmtDelta(d.delta_share_of_voice);
      const app = fmtDelta(d.delta_appearance_rate);
      const cit = fmtDelta(d.delta_citation_rate);
      const rankDisplay = d.delta_avg_rank == null
        ? { text: "–", cls: "flat" } : fmtDelta(-d.delta_avg_rank, false);
      const pillCls = d.brand === run.brand ? "brand" : "comp";
      return `<tr>
        <td><span class="pill ${pillCls}">${d.brand}</span></td>
        <td>${run.products[d.product] ? run.products[d.product].name : d.product}</td>
        <td>${d.llm}</td>
        <td><span class="pill ${sov.cls}">${sov.text}</span></td>
        <td><span class="pill ${app.cls}">${app.text}</span></td>
        <td><span class="pill ${cit.cls}">${cit.text}</span></td>
        <td><span class="pill ${rankDisplay.cls}">${rankDisplay.text}</span></td>
      </tr>`; }).join("")}</tbody></table>`;
}

function renderWebDiff() {
  const run = state.currentRun;
  const productIds = state.selectedProduct === "all" ? Object.keys(run.products) : [state.selectedProduct];
  const c = $("webDiff");
  const parts = productIds.map(pid => {
    const p = run.products[pid];
    if (!p || !p.website) return "";
    const d = p.website.diff || {};
    const hasChanges = d.changed && ((d.added_lines && d.added_lines.length) || (d.removed_lines && d.removed_lines.length));
    return `<details class="prompt-item" ${hasChanges ? "open" : ""}>
      <summary><strong>${p.name}</strong>
        ${d.changed ? `<span class="pill up">Änderungen</span>` : `<span class="pill flat">Unverändert</span>`}
      </summary>
      <p class="hint">${d.summary || ""}</p>
      ${hasChanges ? `<div class="diff-box">
        <div class="added"><strong style="color:var(--success)">Neu (+)</strong><br/>
          ${(d.added_lines || []).slice(0, 50).map(l => `+ ${escapeHtml(l)}`).join("<br/>") || "<em>–</em>"}</div>
        <div class="removed"><strong style="color:var(--danger)">Entfernt (-)</strong><br/>
          ${(d.removed_lines || []).slice(0, 50).map(l => `- ${escapeHtml(l)}`).join("<br/>") || "<em>–</em>"}</div>
      </div>` : ""}</details>`;
  });
  c.innerHTML = parts.join("") || `<p class="hint">Keine Daten.</p>`;
}

function renderPromptDetails() {
  const run = state.currentRun;
  const c = $("promptDetails");
  const productIds = state.selectedProduct === "all" ? Object.keys(run.products) : [state.selectedProduct];
  const llms = state.selectedLLM === "all" ? run.llms : [state.selectedLLM];
  const items = [];
  productIds.forEach(pid => {
    const p = run.products[pid]; if (!p) return;
    p.per_llm.forEach(bundle => {
      if (!llms.includes(bundle.llm)) return;
      bundle.results.forEach(r => items.push({ pid, productName: p.name, llm: bundle.llm, ...r }));
    });
  });
  const show = items.slice(0, 30);
  c.innerHTML = show.map(r => {
    const metrics = r.metrics && r.metrics.brands ? r.metrics.brands : [];
    const pills = metrics.filter(m => m.mentioned).map(m => {
      const cls = m.name === run.brand ? "brand" : "comp";
      const rank = m.first_rank ? ` #${m.first_rank}` : "";
      const cited = m.cited ? " 🔗" : "";
      return `<span class="pill ${cls}">${m.name}${rank}${cited}</span>`;
    }).join(" ");
    return `<details class="prompt-item">
      <summary><span style="opacity:.6">[${r.llm}]</span>
        <strong>${escapeHtml(r.prompt_text)}</strong>
        <span style="opacity:.5;font-size:11px"> — ${r.productName}</span></summary>
      <div class="metric-row">${pills || "<em>Keine Treffer</em>"}</div>
      <div class="prompt-response">${escapeHtml((r.response_text || "").slice(0, 4000))}${(r.response_text || "").length > 4000 ? "..." : ""}</div>
      ${r.sources && r.sources.length ? `<div style="margin-top:10px;font-size:12px;color:var(--text-dim)">
        <strong>Quellen:</strong><br/>${r.sources.slice(0, 8).map(s =>
          `<a href="${s.url}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${s.url}</a>`).join("<br/>")}</div>` : ""}
      </details>`;
  }).join("") + (items.length > 30 ? `<p class="hint center">… ${items.length - 30} weitere (filtere, um weniger anzuzeigen).</p>` : "");
}

function renderDashboard() {
  renderRunMeta();
  renderControls();
  renderKPIs();
  renderExecSummary();
  renderSovChart();
  renderAppearanceChart();
  renderRankChart();
  renderCitationChart();
  renderDeltasTable();
  renderWebDiff();
  renderPromptDetails();
}

// ----------------------------------------------------------------------
// Historie-Tab
// ----------------------------------------------------------------------

async function loadLastNRuns(n) {
  // Nimm die letzten n Runs aus state.runs und lade sie komplett
  const subset = state.runs.slice(-n);
  const loaded = [];
  for (const r of subset) {
    const data = await loadRun(r.file, state.basePath);
    if (data) loaded.push({ meta: r, data });
  }
  return loaded;
}

function renderHistoryTable() {
  const c = $("historyTable");
  if (!state.runs.length) { c.innerHTML = `<p class="hint">Noch keine Läufe vorhanden.</p>`; return; }
  const rows = state.runs.slice().reverse().map(r => {
    const when = r.finished_at ? new Date(r.finished_at).toLocaleString("de-DE") : (r.run_id || "?");
    const cost = r.estimated_cost_usd ? (r.estimated_cost_usd).toFixed(2) + " $" : "–";
    const sov = r.avg_share_of_voice != null ? fmtPct(r.avg_share_of_voice) : "–";
    return `<tr data-file="${r.file}" style="cursor:pointer">
      <td>${when}</td>
      <td>${r.run_id || "–"}</td>
      <td>${r.products_count != null ? r.products_count : "–"}</td>
      <td>${r.llms ? r.llms.join(", ") : "–"}</td>
      <td>${r.prompts_total != null ? r.prompts_total : "–"}</td>
      <td>${cost}</td>
      <td>${sov}</td>
    </tr>`;
  }).join("");
  c.innerHTML = `<table><thead><tr>
    <th>Datum</th><th>Run-ID</th><th>Produkte</th><th>LLMs</th><th>Prompts</th><th>Kosten</th><th>Ø SoV (Marke)</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
  c.querySelectorAll("tr[data-file]").forEach(tr => {
    tr.addEventListener("click", () => {
      state.selectedRunFile = tr.getAttribute("data-file");
      switchTab("dashboard");
      loadAndRenderDashboard();
    });
  });
}

function makeLineChart(canvasId, key, labels, datasets, yLabel, reverse) {
  destroyChart(key);
  state.charts[key] = new Chart($(canvasId), {
    type: "line",
    data: { labels, datasets },
    options: {
      plugins: { legend: { labels: { color: "#e6edf3", boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: "#8b949e" }, grid: { color: "rgba(255,255,255,0.05)" } },
        y: { reverse: !!reverse, beginAtZero: !reverse,
             ticks: { color: "#8b949e" }, grid: { color: "rgba(255,255,255,0.05)" },
             title: { display: !!yLabel, text: yLabel || "", color: "#8b949e" } },
      },
      elements: { line: { tension: 0.3, borderWidth: 2 }, point: { radius: 4 } },
    },
  });
}

async function renderHistory() {
  renderHistoryTable();
  if (state.runs.length < 2) return;
  const runs = await loadLastNRuns(4);
  if (!runs.length) return;

  const labels = runs.map(r => {
    const d = r.data.finished_at ? new Date(r.data.finished_at) : null;
    return d ? d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }) : r.data.run_id;
  });

  // Marken: die Marke aus dem neuesten Run + Top-3 Competitors
  const latest = runs[runs.length - 1].data;
  const brands = [latest.brand, ...((latest.competitors || []).slice(0, 3))];

  function pluck(metricKey, { reverseForRank } = {}) {
    return brands.map((b, i) => {
      const color = b === latest.brand ? BRAND_COLOR : COMP_COLORS[i % COMP_COLORS.length];
      const data = runs.map(r => {
        const agg = aggregate(r.data, "all", "all");
        const row = agg.find(a => a.name === b);
        if (!row) return null;
        if (metricKey === "avg_rank") return row.avg_rank;
        return Math.round(row[metricKey] * 10000) / 100;
      });
      return { label: b, data, borderColor: color, backgroundColor: color, spanGaps: true };
    });
  }

  makeLineChart("trendSovChart", "tsov", labels, pluck("share_of_voice"), "Share of Voice (%)");
  makeLineChart("trendAppChart", "tapp", labels, pluck("appearance_rate"), "Appearance Rate (%)");
  makeLineChart("trendRankChart", "trank", labels, pluck("avg_rank"), "Ø Rang (niedriger = besser)", true);
  makeLineChart("trendCitChart", "tcit", labels, pluck("citation_rate"), "Citation Rate (%)");
}

// ----------------------------------------------------------------------
// Config-Tab
// ----------------------------------------------------------------------

async function loadConfigForEdit() {
  if (state.configLoaded) return;
  const res = await tryFetch(["../data/config.json", "data/config.json"]);
  if (!res) { alert("config.json nicht gefunden."); return; }
  state.config = res.data;

  const basePath = res.path.replace("config.json", "prompts/");
  for (const p of state.config.products) {
    const fn = (p.prompts_file || "").replace(/^prompts\//, "");
    if (!fn) continue;
    const pr = await tryFetch([basePath + fn, "../data/" + p.prompts_file, "data/" + p.prompts_file]);
    if (pr) state.prompts[p.id] = pr.data;
  }

  $("cfgRepo").value = localStorage.getItem("gh_repo") || "phoeser/geo-visibility-tool";
  const savedToken = localStorage.getItem("gh_token");
  if (savedToken) {
    $("cfgToken").value = savedToken;
    $("cfgTokenStatus").textContent = "gesetzt OK";
    $("cfgTokenStatus").className = "pill up";
  }

  $("cfgRepo").addEventListener("change", () => localStorage.setItem("gh_repo", $("cfgRepo").value.trim()));
  $("cfgToken").addEventListener("change", () => {
    const t = $("cfgToken").value.trim();
    if (t) {
      localStorage.setItem("gh_token", t);
      $("cfgTokenStatus").textContent = "gesetzt OK";
      $("cfgTokenStatus").className = "pill up";
    }
  });

  renderConfigUI();
  state.configLoaded = true;
}

function renderConfigUI() {
  const cfg = state.config;
  $("cfgBrandName").value = cfg.brand.name || "";
  $("cfgBrandWebsite").value = cfg.brand.website || "";
  $("cfgBrandDomain").value = cfg.brand.domain || "";
  $("cfgBrandAliases").value = (cfg.brand.aliases || []).join("\n");

  const llmHtml = cfg.llms.map((l, i) => `
    <div class="cfg-item">
      <label class="check-row"><input type="checkbox" data-llm-idx="${i}" ${l.enabled ? "checked" : ""}>
        <strong>${l.display_name || l.id}</strong>
        <span class="hint">(${l.provider} / ${l.model})</span>
      </label>
    </div>
  `).join("");
  $("cfgLlms").innerHTML = llmHtml;

  renderCompetitors();
  renderProducts();
}

function renderCompetitors() {
  const c = $("cfgCompetitors");
  c.innerHTML = state.config.competitors.map((comp, i) => `
    <div class="cfg-item comp-row" data-idx="${i}">
      <div class="cfg-fields">
        <div class="row"><label>Name</label>
          <input type="text" value="${escapeHtml(comp.name)}" data-k="name" data-idx="${i}"></div>
        <div class="row"><label>Domain</label>
          <input type="text" value="${escapeHtml(comp.domain)}" data-k="domain" data-idx="${i}"></div>
        <div class="row"><label>Aliasse <span class="hint">(eine pro Zeile)</span></label>
          <textarea rows="3" data-k="aliases" data-idx="${i}">${escapeHtml((comp.aliases || []).join("\n"))}</textarea></div>
      </div>
      <button class="btn-danger" onclick="cfgRemoveCompetitor(${i})">Entfernen</button>
    </div>
  `).join("");
  c.querySelectorAll("input[data-idx],textarea[data-idx]").forEach(el => {
    el.addEventListener("input", () => {
      const idx = +el.getAttribute("data-idx");
      const k = el.getAttribute("data-k");
      if (k === "aliases") state.config.competitors[idx][k] = el.value.split("\n").map(s => s.trim()).filter(Boolean);
      else state.config.competitors[idx][k] = el.value;
    });
  });
}

function cfgAddCompetitor() {
  state.config.competitors.push({ name: "", aliases: [], domain: "" });
  renderCompetitors();
}
function cfgRemoveCompetitor(i) {
  if (!confirm("Wettbewerber entfernen?")) return;
  state.config.competitors.splice(i, 1);
  renderCompetitors();
}

function renderProducts() {
  const c = $("cfgProducts");
  c.innerHTML = state.config.products.map((p, i) => {
    const prompts = (state.prompts[p.id] && state.prompts[p.id].prompts) || [];
    const promptHtml = prompts.map((pr, j) => `
      <div class="prompt-row" data-pidx="${i}" data-pridx="${j}">
        <input type="text" class="prompt-intent" value="${escapeHtml(pr.intent || "")}" placeholder="Intent"
          data-pidx="${i}" data-pridx="${j}" data-k="intent">
        <input type="text" class="prompt-text" value="${escapeHtml(pr.text || "")}" placeholder="Prompt-Text"
          data-pidx="${i}" data-pridx="${j}" data-k="text">
        <button class="btn-icon" title="Prompt loeschen" onclick="cfgRemovePrompt(${i}, ${j})">X</button>
      </div>
    `).join("");
    return `
      <details class="product-block" ${state._openProduct === p.id ? "open" : ""}>
        <summary>
          <strong>${escapeHtml(p.name || "(unbenannt)")}</strong>
          <span class="hint">- ${prompts.length} Prompts</span>
        </summary>
        <div class="cfg-fields">
          <div class="row"><label>Produkt-ID <span class="hint">(Dateiname, z.B. zahnzusatz)</span></label>
            <input type="text" value="${escapeHtml(p.id || "")}" data-pidx="${i}" data-k="id"></div>
          <div class="row"><label>Name</label>
            <input type="text" value="${escapeHtml(p.name || "")}" data-pidx="${i}" data-k="name"></div>
          <div class="row"><label>Kategorie</label>
            <input type="text" value="${escapeHtml(p.category || "")}" data-pidx="${i}" data-k="category"></div>
          <div class="row"><label>Produkt-URL</label>
            <input type="text" value="${escapeHtml(p.url || "")}" data-pidx="${i}" data-k="url"></div>
        </div>
        <h4 style="margin-top:16px;">Prompts (${prompts.length})</h4>
        <div class="prompts-list">${promptHtml}</div>
        <div class="prompt-actions">
          <button class="btn-secondary" onclick="cfgAddPrompt(${i})">+ Prompt</button>
          <button class="btn-secondary" onclick="cfgGeneratePrompts(${i})">Vorschlaege generieren (Gemini)</button>
          <button class="btn-danger" style="margin-left:auto" onclick="cfgRemoveProduct(${i})">Produkt loeschen</button>
        </div>
      </details>`;
  }).join("");

  c.querySelectorAll("input[data-pidx][data-k]").forEach(el => {
    el.addEventListener("input", () => {
      const pidx = +el.getAttribute("data-pidx");
      const pridx = el.getAttribute("data-pridx");
      const k = el.getAttribute("data-k");
      if (pridx != null) {
        const prod = state.config.products[pidx];
        if (!state.prompts[prod.id]) state.prompts[prod.id] = { product: prod.name, prompts: [] };
        state.prompts[prod.id].prompts[+pridx][k] = el.value;
      } else {
        const old = state.config.products[pidx];
        if (k === "id" && old.id !== el.value) {
          if (state.prompts[old.id]) {
            state.prompts[el.value] = state.prompts[old.id];
            delete state.prompts[old.id];
          }
          old.prompts_file = "prompts/" + el.value + ".json";
        }
        old[k] = el.value;
      }
    });
  });
}

function cfgAddProduct() {
  const newId = "produkt_" + (state.config.products.length + 1);
  state.config.products.push({
    id: newId, name: "Neues Produkt", url: "", category: "",
    prompts_file: "prompts/" + newId + ".json"
  });
  state.prompts[newId] = { product: "Neues Produkt", description: "", prompts: [] };
  state._openProduct = newId;
  renderProducts();
}
function cfgRemoveProduct(i) {
  const p = state.config.products[i];
  if (!confirm("Produkt '" + p.name + "' loeschen?")) return;
  delete state.prompts[p.id];
  state.config.products.splice(i, 1);
  renderProducts();
}
function cfgAddPrompt(pidx) {
  const prod = state.config.products[pidx];
  if (!state.prompts[prod.id]) state.prompts[prod.id] = { product: prod.name, prompts: [] };
  const arr = state.prompts[prod.id].prompts;
  const id = prod.id.slice(0, 2) + "-" + String(arr.length + 1).padStart(2, "0");
  arr.push({ id, intent: "", text: "" });
  state._openProduct = prod.id;
  renderProducts();
}
function cfgRemovePrompt(pidx, pridx) {
  const prod = state.config.products[pidx];
  state.prompts[prod.id].prompts.splice(pridx, 1);
  state._openProduct = prod.id;
  renderProducts();
}

// ----------------------------------------------------------------------
// Gemini-basierte Prompt-Generierung
// ----------------------------------------------------------------------

async function cfgGeneratePrompts(pidx) {
  const prod = state.config.products[pidx];
  const apiKey = localStorage.getItem("google_key");
  if (!apiKey) {
    alert("Google-API-Key nicht gefunden. Bitte im auto_deploy.html Phase 2 eingeben.");
    return;
  }
  if (!prod.name) { alert("Bitte erst Produktnamen eingeben."); return; }

  const brand = state.config.brand.name;
  const compList = state.config.competitors.map(c => c.name).join(", ");

  const metaPrompt = `Du generierst realistische Nutzer-Suchanfragen an ein LLM zu einem Versicherungsprodukt.

Produkt: ${prod.name}
Kategorie: ${prod.category || "Versicherung"}
Marke (zu messen): ${brand}
Konkurrenz: ${compList}

Erstelle genau 20 deutschsprachige Prompts, die echte Kunden an ein LLM stellen wuerden.
Intents verteilen auf: Empfehlung, Vergleich, Top-Liste, Preis, Leistung, Zielgruppe (Rentner, junge Erwachsene, Familien), Markenvergleich, Test-Frage (Stiftung Warentest/Finanztest), Eigenschaften/Bewertungen.

WICHTIG:
- Nicht jeder Prompt soll ${brand} erwaehnen - viele Nutzer nennen keine Marke.
- Natuerliche Alltagssprache.
- Deutsch.

Gib NUR ein JSON-Array zurueck, keine Erklaerungen, keine Code-Fences:
[{"id": "xx-01", "intent": "Empfehlung", "text": "..."}, ...]
Die id als Prefix die ersten zwei Buchstaben der Produkt-ID ("${prod.id.slice(0,2)}"), dann -01 bis -20.`;

  const url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key=" + apiKey;
  const body = {
    contents: [{ parts: [{ text: metaPrompt }] }],
    generationConfig: { temperature: 0.7, maxOutputTokens: 3000 }
  };
  const btn = event.target;
  const orig = btn.textContent;
  btn.disabled = true; btn.textContent = "Generiere ...";
  try {
    const resp = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    });
    if (!resp.ok) throw new Error("Gemini-API: HTTP " + resp.status);
    const data = await resp.json();
    const text = (((data.candidates || [])[0] || {}).content || {}).parts || [];
    let raw = text.map(p => p.text || "").join("");
    raw = raw.replace(/^```(?:json)?/m, "").replace(/```\s*$/m, "").trim();
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) throw new Error("Antwort ist kein Array");
    state.prompts[prod.id] = {
      product: prod.name,
      description: "Realistische Nutzer-Fragen rund um " + prod.name + ". Messen, wie " + brand + " gegenueber " + compList + " erscheint.",
      prompts: arr
    };
    state._openProduct = prod.id;
    renderProducts();
    alert("OK: " + arr.length + " Prompts generiert.");
  } catch (e) {
    alert("Fehler: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = orig;
  }
}

// ----------------------------------------------------------------------
// Speichern (GitHub API)
// ----------------------------------------------------------------------

function collectBrand() {
  state.config.brand.name = $("cfgBrandName").value.trim();
  state.config.brand.website = $("cfgBrandWebsite").value.trim();
  state.config.brand.domain = $("cfgBrandDomain").value.trim();
  state.config.brand.aliases = $("cfgBrandAliases").value.split("\n").map(s => s.trim()).filter(Boolean);
}
function collectLlms() {
  document.querySelectorAll("input[data-llm-idx]").forEach(cb => {
    const i = +cb.getAttribute("data-llm-idx");
    state.config.llms[i].enabled = cb.checked;
  });
}

async function ghRequest(method, url, token, body) {
  const opts = {
    method, headers: {
      "Authorization": "Bearer " + token,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28"
    }
  };
  if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const resp = await fetch(url, opts);
  const text = await resp.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) {}
  return { ok: resp.ok, status: resp.status, json, text };
}

async function ghPutFile(repo, path, contentStr, token, msg) {
  const b64 = btoa(unescape(encodeURIComponent(contentStr)));
  const getUrl = `https://api.github.com/repos/${repo}/contents/${path}?ref=main`;
  const existing = await ghRequest("GET", getUrl, token);
  const body = { message: msg || "Update " + path, content: b64, branch: "main" };
  if (existing.ok && existing.json && existing.json.sha) body.sha = existing.json.sha;
  return await ghRequest("PUT", `https://api.github.com/repos/${repo}/contents/${path}`, token, body);
}

function cfgLog(msg, cls) {
  cls = cls || "info";
  const el = $("cfgSaveLog");
  el.style.display = "block";
  const line = document.createElement("div");
  line.className = "log-line log-" + cls;
  line.textContent = msg;
  el.appendChild(line); el.scrollTop = el.scrollHeight;
}
function cfgStatus(msg, kind) {
  const s = $("cfgSaveStatus");
  s.className = "status show " + (kind || "info");
  s.textContent = msg;
}

async function cfgSaveAll() {
  collectBrand();
  collectLlms();

  const repo = $("cfgRepo").value.trim();
  const token = $("cfgToken").value.trim();
  if (!repo || !token) { cfgStatus("Repo und Token eingeben.", "error"); return; }
  localStorage.setItem("gh_repo", repo);
  localStorage.setItem("gh_token", token);

  for (const p of state.config.products) {
    if (!p.id || !p.name) { cfgStatus("Produkt braucht id + name: " + JSON.stringify(p), "error"); return; }
  }

  const btn = $("cfgSaveBtn");
  btn.disabled = true; btn.textContent = "Speichere ...";
  $("cfgSaveLog").innerHTML = "";
  cfgStatus("Speichere config.json und Prompt-Dateien ...", "info");

  try {
    const cfgJson = JSON.stringify(state.config, null, 2);
    const r1 = await ghPutFile(repo, "data/config.json", cfgJson, token, "chore: update config via dashboard");
    if (!r1.ok && r1.status !== 201) throw new Error("config.json: HTTP " + r1.status);
    cfgLog("  OK data/config.json", "ok");

    for (const p of state.config.products) {
      const data = state.prompts[p.id];
      if (!data) { cfgLog("  skip " + p.id + " (keine Prompts)", "warn"); continue; }
      const path = "data/" + (p.prompts_file || ("prompts/" + p.id + ".json"));
      const content = JSON.stringify(data, null, 2);
      const r = await ghPutFile(repo, path, content, token, "chore: update prompts " + p.id);
      if (!r.ok && r.status !== 201) { cfgLog("  FAIL " + path + " HTTP " + r.status, "err"); continue; }
      cfgLog("  OK " + path, "ok");
    }

    cfgLog("", "info");
    cfgLog("Fertig - Aenderungen sind im Repo. Naechster Lauf nutzt die neue Config.", "ok");
    cfgLog("Workflow manuell starten: github.com/" + repo + "/actions", "info");
    cfgStatus("Erfolgreich gespeichert.", "ok");
  } catch (e) {
    cfgLog("  FAIL " + e.message, "err");
    cfgStatus("Fehler: " + e.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "Alle Aenderungen speichern";
  }
}

// ----------------------------------------------------------------------
// Refresh-Button (Workflow Dispatch)
// ----------------------------------------------------------------------

async function triggerRefresh() {
  const btn = $("refreshBtn");
  const token = localStorage.getItem("gh_token");
  const repo = (localStorage.getItem("gh_repo") || "").trim();
  if (!token || !repo) {
    alert("Bitte zuerst im Config-Tab GitHub-Repo und Token setzen.");
    switchTab("config");
    return;
  }
  if (!confirm("Neuen Analyse-Lauf starten? Das ruft den GitHub-Actions-Workflow auf und kann einige Minuten dauern.")) return;

  btn.disabled = true;
  btn.classList.add("is-loading");
  const oldText = btn.textContent;
  if (btn.firstChild) btn.firstChild.nodeValue = "Starte ...";

  try {
    const url = "https://api.github.com/repos/" + repo + "/actions/workflows/analyze.yml/dispatches";
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    if (res.status !== 204) {
      const err = await res.text();
      throw new Error("HTTP " + res.status + ": " + err.slice(0, 200));
    }
    if (btn.firstChild) btn.firstChild.nodeValue = "OK Lauf gestartet";
    btn.classList.remove("is-loading");
    setTimeout(function () {
      if (btn.firstChild) btn.firstChild.nodeValue = oldText;
      btn.disabled = false;
    }, 5000);
    const repoUrl = "https://github.com/" + repo + "/actions";
    if (confirm("Workflow laeuft jetzt. Moechtest du den Fortschritt auf GitHub oeffnen?")) {
      window.open(repoUrl, "_blank", "noopener");
    }
  } catch (e) {
    btn.classList.remove("is-loading");
    if (btn.firstChild) btn.firstChild.nodeValue = oldText;
    btn.disabled = false;
    alert("Fehler beim Starten:\n" + e.message);
  }
}

// ----------------------------------------------------------------------
// Tab-Navigation + Init
// ----------------------------------------------------------------------

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-tab") === name);
  });
  document.querySelectorAll(".tab-panel").forEach(function (p) {
    p.classList.toggle("active", p.id === "tab-" + name);
  });
  if (name === "history") renderHistory();
  if (name === "config") loadConfigForEdit();
}

async function init() {
  document.querySelectorAll(".tab-btn").forEach(function (b) {
    b.addEventListener("click", function () { switchTab(b.getAttribute("data-tab")); });
  });

  const idx = await loadIndex();
  if (!idx || !idx.runs.length) {
    $("runMeta").textContent = "Noch keine Laeufe. Starte einen Lauf ueber den Refresh-Button.";
    return;
  }
  state.runs = idx.runs;
  state.basePath = idx.basePath;
  state.selectedRunFile = idx.runs[idx.runs.length - 1].file;
  await loadAndRenderDashboard();

  $("runSelector").addEventListener("change", async function (e) {
    state.selectedRunFile = e.target.value;
    await loadAndRenderDashboard();
  });
  $("productSelector").addEventListener("change", function (e) {
    state.selectedProduct = e.target.value; renderDashboard();
  });
  $("llmSelector").addEventListener("change", function (e) {
    state.selectedLLM = e.target.value; renderDashboard();
  });
}

async function loadAndRenderDashboard() {
  const run = await loadRun(state.selectedRunFile, state.basePath);
  if (!run) return;
  state.currentRun = run;
  renderDashboard();
}

init();
