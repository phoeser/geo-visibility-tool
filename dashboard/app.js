/* ----------------------------------------------------------------------
 * GEO Visibility Dashboard
 * Liest die JSON-Dateien aus ../data/runs/ (relativer Pfad zur Seite).
 * Funktioniert sowohl unter GitHub Pages (wenn /dashboard als Root gesetzt
 * wird) als auch lokal via `python -m http.server` im Projekt-Root.
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
};

// ----------------------------------------------------------------------
// Data Loading
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
  // Versuche beide Pfade: dashboard/ als Root UND Projekt-Root
  const candidates = [
    "../data/runs/index.json",
    "data/runs/index.json",
  ];
  const res = await tryFetch(candidates);
  return res ? { runs: res.data.runs || [], basePath: res.path.replace("index.json", "") } : null;
}

async function loadRun(file, basePath) {
  const res = await tryFetch([basePath + file]);
  return res ? res.data : null;
}

// ----------------------------------------------------------------------
// Rendering helpers
// ----------------------------------------------------------------------

function fmtPct(v) {
  if (v === null || v === undefined) return "–";
  return (v * 100).toFixed(1) + " %";
}
function fmtNum(v, digits = 2) {
  if (v === null || v === undefined) return "–";
  return Number(v).toFixed(digits);
}
function fmtDelta(v, isPct = true) {
  if (v === null || v === undefined) return { text: "–", cls: "flat" };
  const pretty = isPct ? (v * 100).toFixed(1) + " %-Pt" : v.toFixed(2);
  if (v > 0.0005) return { text: "▲ " + pretty, cls: "up" };
  if (v < -0.0005) return { text: "▼ " + pretty.replace("-", ""), cls: "down" };
  return { text: "– " + pretty, cls: "flat" };
}

function destroyChart(key) {
  if (state.charts[key]) {
    state.charts[key].destroy();
    delete state.charts[key];
  }
}

// ----------------------------------------------------------------------
// Aggregation über aktuelles Filter-Set
// ----------------------------------------------------------------------

function aggregate() {
  const run = state.currentRun;
  if (!run) return null;

  const productIds = state.selectedProduct === "all"
    ? Object.keys(run.products)
    : [state.selectedProduct];

  const llms = state.selectedLLM === "all" ? run.llms : [state.selectedLLM];
  const brandOrder = [run.brand, ...(run.competitors || [])];

  const totals = {};
  brandOrder.forEach(n => totals[n] = {
    mentions: 0, appearances: 0, prompts: 0, citations: 0, ranks: [],
  });

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
        if (b.avg_rank !== null && b.avg_rank !== undefined) {
          totals[b.name].ranks.push(b.avg_rank);
        }
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
// UI-Rendering
// ----------------------------------------------------------------------

function renderRunMeta() {
  const run = state.currentRun;
  if (!run) {
    document.getElementById("runMeta").textContent = "Keine Daten";
    return;
  }
  const when = run.finished_at ? new Date(run.finished_at).toLocaleString("de-DE") : "?";
  document.getElementById("runMeta").innerHTML =
    `<strong>${run.brand}</strong> — Lauf ${run.run_id} • ${when} • LLMs: ${run.llms.join(", ")}`;
}

function renderControls() {
  const run = state.currentRun;

  const prod = document.getElementById("productSelector");
  prod.innerHTML = '<option value="all">Alle Produkte</option>';
  Object.entries(run.products).forEach(([id, p]) => {
    prod.insertAdjacentHTML("beforeend", `<option value="${id}">${p.name}</option>`);
  });
  prod.value = state.selectedProduct;

  const llm = document.getElementById("llmSelector");
  llm.innerHTML = '<option value="all">Alle LLMs</option>';
  run.llms.forEach(id => {
    llm.insertAdjacentHTML("beforeend", `<option value="${id}">${id}</option>`);
  });
  llm.value = state.selectedLLM;

  const runs = document.getElementById("runSelector");
  runs.innerHTML = "";
  state.runs.slice().reverse().forEach(r => {
    const opt = document.createElement("option");
    opt.value = r.file;
    opt.textContent = `${r.run_id}`;
    if (r.file === state.selectedRunFile) opt.selected = true;
    runs.appendChild(opt);
  });
}

function renderKPIs() {
  const agg = aggregate();
  const run = state.currentRun;
  const brand = run.brand;
  const brandRow = agg.find(a => a.name === brand);
  if (!brandRow) return;

  // Gesamt-Rang
  const ranked = agg.slice().sort((a, b) => b.share_of_voice - a.share_of_voice);
  const brandPos = ranked.findIndex(r => r.name === brand) + 1;

  // Delta-Daten aus impact
  const deltas = (run.impact && run.impact.deltas && run.impact.deltas.changes) || [];
  const brandDeltas = deltas.filter(d => d.brand === brand &&
    (state.selectedProduct === "all" || d.product === state.selectedProduct) &&
    (state.selectedLLM === "all" || d.llm === state.selectedLLM));
  const avg = (key) => brandDeltas.length
    ? brandDeltas.reduce((a, b) => a + (b[key] || 0), 0) / brandDeltas.length
    : null;

  const kpis = [
    {
      label: "Share of Voice",
      value: fmtPct(brandRow.share_of_voice),
      delta: fmtDelta(avg("delta_share_of_voice")),
    },
    {
      label: "Nennungs-Quote",
      value: fmtPct(brandRow.appearance_rate),
      delta: fmtDelta(avg("delta_appearance_rate")),
    },
    {
      label: "Zitierungs-Quote",
      value: fmtPct(brandRow.citation_rate),
      delta: fmtDelta(avg("delta_citation_rate")),
    },
    {
      label: "Ø Rang in Listen",
      value: fmtNum(brandRow.avg_rank, 2),
      delta: fmtDelta(avg("delta_avg_rank") ? -avg("delta_avg_rank") : null, false),
    },
    {
      label: "Position im Markt",
      value: brandPos + " / " + agg.length,
      delta: { text: "unter " + agg.length + " Marken", cls: "flat" },
    },
  ];

  const row = document.getElementById("kpiRow");
  row.innerHTML = kpis.map(k => `
    <div class="kpi">
      <div class="label">${k.label}</div>
      <div class="value">${k.value}</div>
      <div class="delta ${k.delta.cls}">${k.delta.text}</div>
    </div>`).join("");
}

function renderExecSummary() {
  const run = state.currentRun;
  const text = (run.impact && run.impact.executive_summary) || "Noch keine Zusammenfassung verfügbar.";
  document.getElementById("execSummary").textContent = text;
}

function renderSovChart() {
  const agg = aggregate();
  const labels = agg.map(a => a.name);
  const values = agg.map(a => Math.round(a.share_of_voice * 10000) / 100);
  const colors = agg.map(a => a.name === state.currentRun.brand ? BRAND_COLOR
                                        : COMP_COLORS[labels.indexOf(a.name) % COMP_COLORS.length]);
  destroyChart("sov");
  state.charts.sov = new Chart(document.getElementById("sovChart"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Share of Voice (%)",
        data: values,
        backgroundColor: colors,
        borderRadius: 6,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, ticks: { color: "#8b949e" },
             grid: { color: "rgba(255,255,255,0.05)" } },
        x: { ticks: { color: "#e6edf3" }, grid: { display: false } },
      },
    },
  });
}

function renderAppearanceChart() {
  const agg = aggregate();
  const labels = agg.map(a => a.name);
  const values = agg.map(a => Math.round(a.appearance_rate * 10000) / 100);
  destroyChart("app");
  state.charts.app = new Chart(document.getElementById("appearanceChart"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Appearance Rate (%)",
        data: values,
        backgroundColor: labels.map((n, i) =>
          n === state.currentRun.brand ? BRAND_COLOR : COMP_COLORS[i % COMP_COLORS.length]),
        borderRadius: 6,
      }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { beginAtZero: true, ticks: { color: "#8b949e" },
             grid: { color: "rgba(255,255,255,0.05)" } },
        y: { ticks: { color: "#e6edf3" }, grid: { display: false } },
      },
    },
  });
}

function renderRankChart() {
  const agg = aggregate();
  const labels = agg.map(a => a.name);
  const values = agg.map(a => a.avg_rank !== null ? a.avg_rank : null);
  destroyChart("rank");
  state.charts.rank = new Chart(document.getElementById("rankChart"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Ø Rang (niedriger = besser)",
        data: values,
        backgroundColor: labels.map((n, i) =>
          n === state.currentRun.brand ? BRAND_COLOR : COMP_COLORS[i % COMP_COLORS.length]),
        borderRadius: 6,
      }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { beginAtZero: true, reverse: false, ticks: { color: "#8b949e" },
             grid: { color: "rgba(255,255,255,0.05)" } },
        y: { ticks: { color: "#e6edf3" }, grid: { display: false } },
      },
    },
  });
}

function renderCitationChart() {
  const agg = aggregate();
  const labels = agg.map(a => a.name);
  const values = agg.map(a => Math.round(a.citation_rate * 10000) / 100);
  destroyChart("cit");
  state.charts.cit = new Chart(document.getElementById("citationChart"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Citation Rate (%)",
        data: values,
        backgroundColor: labels.map((n, i) =>
          n === state.currentRun.brand ? BRAND_COLOR : COMP_COLORS[i % COMP_COLORS.length]),
        borderRadius: 6,
      }],
    },
    options: {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: { beginAtZero: true, ticks: { color: "#8b949e" },
             grid: { color: "rgba(255,255,255,0.05)" } },
        y: { ticks: { color: "#e6edf3" }, grid: { display: false } },
      },
    },
  });
}

function renderDeltasTable() {
  const run = state.currentRun;
  const deltas = (run.impact && run.impact.deltas && run.impact.deltas.changes) || [];
  const filtered = deltas.filter(d =>
    (state.selectedProduct === "all" || d.product === state.selectedProduct) &&
    (state.selectedLLM === "all" || d.llm === state.selectedLLM));
  const sorted = filtered.slice().sort((a, b) =>
    Math.abs(b.delta_share_of_voice || 0) - Math.abs(a.delta_share_of_voice || 0)
  ).slice(0, 10);

  const c = document.getElementById("deltasTable");
  if (!sorted.length) {
    c.innerHTML = `<p style="color:var(--text-dim)">Keine vergleichbaren Daten vorhanden — vermutlich der erste Lauf.</p>`;
    return;
  }

  c.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Marke</th><th>Produkt</th><th>LLM</th>
          <th>ΔShare of Voice</th><th>ΔNennungs-Quote</th>
          <th>ΔZitierung</th><th>ΔØ Rang</th>
        </tr>
      </thead>
      <tbody>
        ${sorted.map(d => {
          const sov = fmtDelta(d.delta_share_of_voice);
          const app = fmtDelta(d.delta_appearance_rate);
          const cit = fmtDelta(d.delta_citation_rate);
          // Rang: niedriger ist besser → Vorzeichen drehen für Pfeil
          const rankDisplay = d.delta_avg_rank === null || d.delta_avg_rank === undefined
            ? { text: "–", cls: "flat" }
            : fmtDelta(-d.delta_avg_rank, false);
          const pillCls = d.brand === run.brand ? "brand" : "comp";
          return `
          <tr>
            <td><span class="pill ${pillCls}">${d.brand}</span></td>
            <td>${run.products[d.product] ? run.products[d.product].name : d.product}</td>
            <td>${d.llm}</td>
            <td><span class="pill ${sov.cls}">${sov.text}</span></td>
            <td><span class="pill ${app.cls}">${app.text}</span></td>
            <td><span class="pill ${cit.cls}">${cit.text}</span></td>
            <td><span class="pill ${rankDisplay.cls}">${rankDisplay.text}</span></td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

function renderWebDiff() {
  const run = state.currentRun;
  const productIds = state.selectedProduct === "all"
    ? Object.keys(run.products) : [state.selectedProduct];
  const c = document.getElementById("webDiff");
  const parts = productIds.map(pid => {
    const p = run.products[pid];
    if (!p || !p.website) return "";
    const d = p.website.diff || {};
    const hasChanges = d.changed && (d.added_lines?.length || d.removed_lines?.length);
    return `
      <details class="prompt-item" ${hasChanges ? "open" : ""}>
        <summary>
          <strong>${p.name}</strong>
          ${d.changed ? `<span class="pill up">Änderungen</span>` :
                       `<span class="pill flat">Unverändert</span>`}
        </summary>
        <p style="color:var(--text-dim);margin:10px 0">${d.summary || ""}</p>
        ${hasChanges ? `
          <div class="diff-box">
            <div class="added">
              <strong style="color:var(--success)">Neu (+)</strong><br/>
              ${(d.added_lines || []).slice(0,50).map(l => `+ ${escapeHtml(l)}`).join("<br/>") || "<em>–</em>"}
            </div>
            <div class="removed">
              <strong style="color:var(--danger)">Entfernt (-)</strong><br/>
              ${(d.removed_lines || []).slice(0,50).map(l => `- ${escapeHtml(l)}`).join("<br/>") || "<em>–</em>"}
            </div>
          </div>` : ""}
      </details>`;
  });
  c.innerHTML = parts.join("") || `<p style="color:var(--text-dim)">Keine Daten.</p>`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (m) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
  ));
}

function renderPromptDetails() {
  const run = state.currentRun;
  const c = document.getElementById("promptDetails");
  const productIds = state.selectedProduct === "all"
    ? Object.keys(run.products) : [state.selectedProduct];
  const llms = state.selectedLLM === "all" ? run.llms : [state.selectedLLM];

  const items = [];
  productIds.forEach(pid => {
    const p = run.products[pid];
    if (!p) return;
    p.per_llm.forEach(bundle => {
      if (!llms.includes(bundle.llm)) return;
      bundle.results.forEach(r => {
        items.push({ pid, productName: p.name, llm: bundle.llm, ...r });
      });
    });
  });

  // Auf 30 begrenzen, sonst wird die Seite zu schwer
  const show = items.slice(0, 30);
  c.innerHTML = show.map(r => {
    const metrics = r.metrics && r.metrics.brands ? r.metrics.brands : [];
    const pills = metrics.filter(m => m.mentioned).map(m => {
      const cls = m.name === run.brand ? "brand" : "comp";
      const rank = m.first_rank ? ` #${m.first_rank}` : "";
      const cited = m.cited ? " 🔗" : "";
      return `<span class="pill ${cls}">${m.name}${rank}${cited}</span>`;
    }).join(" ");
    return `
      <details class="prompt-item">
        <summary>
          <span style="opacity:.6">[${r.llm}]</span>
          <strong>${escapeHtml(r.prompt_text)}</strong>
          <span style="opacity:.5;font-size:11px"> — ${r.productName}</span>
        </summary>
        <div class="metric-row">${pills || "<em>Keine Treffer</em>"}</div>
        <div class="prompt-response">${escapeHtml((r.response_text || "").slice(0, 4000))}${(r.response_text||"").length > 4000 ? "..." : ""}</div>
        ${r.sources && r.sources.length ? `
          <div style="margin-top:10px;font-size:12px;color:var(--text-dim)">
            <strong>Quellen:</strong><br/>
            ${r.sources.slice(0,8).map(s =>
              `<a href="${s.url}" target="_blank" rel="noopener noreferrer" style="color:var(--accent)">${s.url}</a>`
            ).join("<br/>")}
          </div>` : ""}
      </details>`;
  }).join("") + (items.length > 30 ?
    `<p style="color:var(--text-dim);text-align:center;margin-top:14px">
      … ${items.length - 30} weitere Einträge (filtere, um weniger anzuzeigen).</p>` : "");
}

// ----------------------------------------------------------------------
// Main
// ----------------------------------------------------------------------

async function init() {
  const idx = await loadIndex();
  if (!idx || !idx.runs.length) {
    document.getElementById("runMeta").textContent =
      "Noch keine Läufe vorhanden. Starten Sie einen Lauf im GitHub-Actions-Tab.";
    return;
  }
  state.runs = idx.runs;
  state.basePath = idx.basePath;
  state.selectedRunFile = idx.runs[idx.runs.length - 1].file;
  await loadAndRender();

  document.getElementById("runSelector").addEventListener("change", async (e) => {
    state.selectedRunFile = e.target.value;
    await loadAndRender();
  });
  document.getElementById("productSelector").addEventListener("change", (e) => {
    state.selectedProduct = e.target.value;
    renderAll();
  });
  document.getElementById("llmSelector").addEventListener("change", (e) => {
    state.selectedLLM = e.target.value;
    renderAll();
  });
}

async function loadAndRender() {
  const run = await loadRun(state.selectedRunFile, state.basePath);
  if (!run) return;
  state.currentRun = run;
  renderAll();
}

function renderAll() {
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

init();
