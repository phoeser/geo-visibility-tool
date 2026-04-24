// sync-trigger-2026-04-23-b
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
  const c = $("webDiff");
  const pt = (run && run.page_tracking) || null;
  const events = (pt && Array.isArray(pt.events_this_run)) ? pt.events_this_run : [];

  if (!events.length) {
    c.innerHTML = `<p class="hint">Keine Page-Tracking-Events im aktuellen Lauf.</p>`;
    return;
  }

  // Nur interessante Events (Änderungen + Erstsichtungen), Filter fürs gewählte Produkt
  const selectedPid = state.selectedProduct;
  function matchProduct(e) {
    if (!selectedPid || selectedPid === "all") return true;
    return (e.product_ids || []).includes(selectedPid);
  }
  const interesting = events.filter(e => (e.changed || e.first_seen) && matchProduct(e));

  // Counts pro Art
  const nChanged = interesting.filter(e => e.changed).length;
  const nFirst = interesting.filter(e => e.first_seen).length;
  const nErrors = events.filter(e => e.error && matchProduct(e)).length;
  const nUnchanged = events.filter(e => !e.changed && !e.first_seen && !e.error && matchProduct(e)).length;

  // Gruppieren nach Marke
  const byBrand = {};
  for (const e of interesting) {
    const b = e.brand || "–";
    (byBrand[b] = byBrand[b] || []).push(e);
  }

  // Innerhalb einer Marke sortieren: "changed" zuerst, dann nach kleinster Similarity (größter Diff)
  for (const b of Object.keys(byBrand)) {
    byBrand[b].sort((a, b2) => {
      if (a.changed !== b2.changed) return a.changed ? -1 : 1;
      return (a.similarity || 1) - (b2.similarity || 1);
    });
  }

  const headerHtml = `
    <div class="diff-summary">
      <span class="pill up">${nChanged} Änderung${nChanged === 1 ? "" : "en"}</span>
      <span class="pill flat">${nFirst} Erstsichtung${nFirst === 1 ? "" : "en"}</span>
      <span class="pill flat">${nUnchanged} unverändert</span>
      ${nErrors ? `<span class="pill down">${nErrors} Fehler</span>` : ""}
    </div>
  `;

  function diffRow(e) {
    const kind = e.changed ? "change" : (e.first_seen ? "first_seen" : "other");
    const pill = e.changed
      ? `<span class="pill up">Änderung</span>`
      : `<span class="pill flat">Erstsichtung</span>`;
    const sim = (e.similarity !== null && e.similarity !== undefined)
      ? (e.similarity * 100).toFixed(1) + " %" : "–";
    const url = e.url || "";
    const urlShort = url.length > 90 ? url.slice(0, 90) + "…" : url;
    const pids = (e.product_ids || []).join(", ");
    const added = (e.added_lines || []).slice(0, 30);
    const removed = (e.removed_lines || []).slice(0, 30);
    const cls = (e.classification || {});
    const clsHtml = cls.category || cls.type
      ? `<span class="pill flat">${escapeHtml(cls.category || cls.type)}</span>` : "";
    return `
      <details class="diff-event ${kind}">
        <summary>
          ${pill} ${clsHtml}
          <span class="diff-url" title="${escapeHtml(url)}"><a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(urlShort)}</a></span>
          <span class="hint">${escapeHtml(pids)} · Ähnlichkeit ${sim} · +${e.added_lines ? e.added_lines.length : 0} / −${e.removed_lines ? e.removed_lines.length : 0}</span>
        </summary>
        ${e.summary ? `<p class="hint" style="margin: 4px 0 8px 0;">${escapeHtml(e.summary)}</p>` : ""}
        ${(cls.reasoning || cls.summary) ? `<p class="hint" style="margin: 0 0 8px 0;"><em>Gemini:</em> ${escapeHtml(cls.reasoning || cls.summary)}</p>` : ""}
        ${(added.length || removed.length) ? `
          <div class="diff-box">
            <div class="added"><strong style="color:var(--success)">Neu (+)</strong><br/>
              ${added.map(l => "+ " + escapeHtml(l)).join("<br/>") || "<em>–</em>"}
            </div>
            <div class="removed"><strong style="color:var(--danger)">Entfernt (−)</strong><br/>
              ${removed.map(l => "− " + escapeHtml(l)).join("<br/>") || "<em>–</em>"}
            </div>
          </div>` : ""}
      </details>`;
  }

  const brandsOrder = Object.keys(byBrand).sort((a, b) => byBrand[b].length - byBrand[a].length);
  const bodyHtml = brandsOrder.map(brand => {
    const rows = byBrand[brand].map(diffRow).join("");
    return `
      <details class="diff-brand" open>
        <summary><strong>${escapeHtml(brand)}</strong>
          <span class="hint">— ${byBrand[brand].length} Event${byBrand[brand].length === 1 ? "" : "s"}</span>
        </summary>
        <div class="diff-events">${rows}</div>
      </details>`;
  }).join("");

  c.innerHTML = headerHtml + (bodyHtml || `<p class="hint">Keine interessanten Events für das gewählte Produkt.</p>`);
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

function summarizeRunForTable(runData) {
  // Berechnet Produkte-Anzahl, Prompts-Total und Ø SoV (Marke) aus einem voll geladenen Run.
  if (!runData) return { products_count: null, prompts_total: null, sov: null };
  const pids = Object.keys(runData.products || {});
  let prompts_total = 0;
  let sovNum = 0, sovCount = 0;
  pids.forEach(pid => {
    const p = runData.products[pid];
    (runData.llms || []).forEach(llm => {
      const sum = p.summary_by_llm && p.summary_by_llm[llm];
      if (!sum) return;
      prompts_total += sum.prompts_total || 0;
      const brandRow = (sum.brands || []).find(b => b.name === runData.brand);
      if (brandRow && typeof brandRow.share_of_voice === "number") {
        sovNum += brandRow.share_of_voice;
        sovCount += 1;
      }
    });
  });
  return {
    products_count: pids.length || null,
    prompts_total: prompts_total || null,
    sov: sovCount ? (sovNum / sovCount) : null,
  };
}

function renderHistoryTable(extraByFile) {
  const c = $("historyTable");
  if (!state.runs.length) { c.innerHTML = `<p class="hint">Noch keine Läufe vorhanden.</p>`; return; }
  const extra = extraByFile || {};
  const rows = state.runs.slice().reverse().map(r => {
    const when = r.finished_at ? new Date(r.finished_at).toLocaleString("de-DE") : (r.run_id || "?");
    const ex = extra[r.file] || {};
    // Fallback-Werte aus dem Index, dann aus den vollständig geladenen Runs, dann "–"
    const productsCount = (r.products && r.products.length)
      || ex.products_count
      || (r.products_count != null ? r.products_count : null);
    const promptsTotal = ex.prompts_total != null ? ex.prompts_total : (r.prompts_total != null ? r.prompts_total : null);
    const sovVal = ex.sov != null ? ex.sov : (r.avg_share_of_voice != null ? r.avg_share_of_voice : null);
    const cost = r.estimated_cost_usd ? (r.estimated_cost_usd).toFixed(2) + " $" : "–";
    return `<tr data-file="${r.file}" style="cursor:pointer">
      <td>${when}</td>
      <td>${r.run_id || "–"}</td>
      <td>${productsCount != null ? productsCount : "–"}</td>
      <td>${r.llms ? r.llms.join(", ") : "–"}</td>
      <td>${promptsTotal != null ? promptsTotal : "–"}</td>
      <td>${cost}</td>
      <td>${sovVal != null ? fmtPct(sovVal) : "–"}</td>
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

function isoWeekOf(date) {
  // Liefert "YYYY-Www" (ISO-Woche) fuer ein Datum.
  const d = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate()));
  const dayNum = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() + 4 - dayNum);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const weekNo = Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
  return d.getUTCFullYear() + "-W" + String(weekNo).padStart(2, "0");
}

function bucketRunsByWeek(loaded) {
  // loaded: [{meta, data}, ...] chronologisch aufsteigend
  // Returns: [{label, runs: [...]}]  ein Eintrag je Woche
  const byWeek = new Map();
  const order = [];
  for (const r of loaded) {
    const t = r.data.finished_at || r.data.started_at || r.meta.finished_at || r.meta.started_at;
    if (!t) continue;
    const wk = isoWeekOf(new Date(t));
    if (!byWeek.has(wk)) { byWeek.set(wk, []); order.push(wk); }
    byWeek.get(wk).push(r);
  }
  return order.map(wk => ({ label: "KW " + wk.slice(-2) + " / " + wk.slice(2, 4), runs: byWeek.get(wk) }));
}

async function renderHistory() {
  renderHistoryTable();

  const total = state.runs.length;
  // Cap: bis zu 50 Runs laden (neueste 50)
  const maxLoad = Math.min(total, 50);
  const subset = state.runs.slice(-maxLoad);
  const extra = {};
  const loaded = [];
  for (const r of subset) {
    const d = await loadRun(r.file, state.basePath);
    if (d) {
      loaded.push({ meta: r, data: d });
      extra[r.file] = summarizeRunForTable(d);
    }
  }
  renderHistoryTable(extra);

  if (loaded.length < 2) return;

  // Aggregation-Entscheidung: ab 50 Eintraegen wird pro Woche gebuendelt.
  const weekly = loaded.length >= 50;
  const latest = loaded[loaded.length - 1].data;
  const brands = [latest.brand, ...((latest.competitors || []).slice(0, 3))].filter(Boolean);

  let labels;
  let groups; // Array von Arrays von Runs (pro Bucket)

  if (weekly) {
    const buckets = bucketRunsByWeek(loaded);
    labels = buckets.map(b => b.label);
    groups = buckets.map(b => b.runs);
  } else {
    labels = loaded.map(r => {
      const d = r.data.finished_at ? new Date(r.data.finished_at) : null;
      return d ? d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }) : r.data.run_id;
    });
    groups = loaded.map(r => [r]);
  }

  function aggregateBucket(runsInBucket, brand, metricKey) {
    // Mittelwert je Bucket ueber alle Runs
    const vals = [];
    for (const r of runsInBucket) {
      const agg = aggregate(r.data, "all", "all");
      const row = agg.find(a => a.name === brand);
      if (!row) continue;
      const v = row[metricKey];
      if (v === null || v === undefined || isNaN(v)) continue;
      vals.push(v);
    }
    if (!vals.length) return null;
    return vals.reduce((a, b) => a + b, 0) / vals.length;
  }

  function pluck(metricKey) {
    return brands.map((b, i) => {
      const color = b === latest.brand ? BRAND_COLOR : COMP_COLORS[i % COMP_COLORS.length];
      const data = groups.map(bucket => {
        const v = aggregateBucket(bucket, b, metricKey);
        if (v === null) return null;
        if (metricKey === "avg_rank") return v;
        return Math.round(v * 10000) / 100;
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
  // Marken-Liste fuer tracked_urls: eigene + Wettbewerber
  const brandList = [state.config.brand.name].concat(
    (state.config.competitors || []).map(c => c.name)
  ).filter(Boolean);

  c.innerHTML = state.config.products.map((p, i) => {
    const prompts = (state.prompts[p.id] && state.prompts[p.id].prompts) || [];
    // Sicherstellen dass keywords + tracked_urls existieren
    if (!Array.isArray(p.keywords)) p.keywords = [];
    if (!p.tracked_urls || typeof p.tracked_urls !== "object") p.tracked_urls = {};

    const promptHtml = prompts.map((pr, j) => `
      <div class="prompt-row" data-pidx="${i}" data-pridx="${j}">
        <input type="text" class="prompt-intent" value="${escapeHtml(pr.intent || "")}" placeholder="Intent"
          data-pidx="${i}" data-pridx="${j}" data-k="intent">
        <input type="text" class="prompt-text" value="${escapeHtml(pr.text || "")}" placeholder="Prompt-Text"
          data-pidx="${i}" data-pridx="${j}" data-k="text">
        <button class="btn-icon" title="Prompt loeschen" onclick="cfgRemovePrompt(${i}, ${j})">X</button>
      </div>
    `).join("");

    const trackedHtml = brandList.map(brand => {
      const urls = Array.isArray(p.tracked_urls[brand]) ? p.tracked_urls[brand] : [];
      const placeholder = urls.length === 0
        ? "(leer — bei Auto-Discovery wird per Sitemap nach Keywords gesucht)"
        : "";
      return `
        <div class="url-block" data-brand="${escapeHtml(brand)}">
          <label>
            <strong>${escapeHtml(brand)}</strong>
            <span class="hint">— ${urls.length} URL${urls.length === 1 ? "" : "s"}</span>
          </label>
          <textarea
            class="url-textarea"
            rows="3"
            data-pidx="${i}"
            data-brand="${escapeHtml(brand)}"
            placeholder="${placeholder || "Eine URL pro Zeile, z.B. https://www.ergo.de/de/Produkte/..."}"
          >${escapeHtml(urls.join("\n"))}</textarea>
        </div>`;
    }).join("");

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
          <div class="row"><label>Produkt-URL <span class="hint">(Legacy-Fallback)</span></label>
            <input type="text" value="${escapeHtml(p.url || "")}" data-pidx="${i}" data-k="url"></div>
        </div>

        <h4 style="margin-top:18px;">Keywords für Auto-Discovery</h4>
        <p class="hint">
          Kommagetrennt oder eine pro Zeile. Werden genutzt, um für jede Marke ohne manuelle URL-Liste
          passende Seiten aus der Sitemap zu finden (max. 15 pro Marke).
        </p>
        <textarea
          class="kw-textarea"
          rows="2"
          data-pidx="${i}"
          data-k="keywords"
          placeholder="zahnzusatz, zahnzusatzversicherung, zahnersatz"
        >${escapeHtml((p.keywords || []).join(", "))}</textarea>

        <h4 style="margin-top:18px;">URL-Tracking pro Marke</h4>
        <p class="hint">
          Wenn leer: Auto-Discovery über Sitemap &amp; Keywords. Wenn gefüllt: genau diese URLs werden
          für die Marke gescraped (überschreibt Auto-Discovery).
        </p>
        <div class="url-tracking">${trackedHtml}</div>

        <h4 style="margin-top:18px;">Prompts (${prompts.length})</h4>
        <div class="prompts-list">${promptHtml}</div>
        <div class="prompt-actions">
          <button class="btn-secondary" onclick="cfgAddPrompt(${i})">+ Prompt</button>
          <button class="btn-secondary" onclick="cfgGeneratePrompts(${i})">Vorschlaege generieren (Gemini)</button>
          <button class="btn-danger" style="margin-left:auto" onclick="cfgRemoveProduct(${i})">Produkt loeschen</button>
        </div>
      </details>`;
  }).join("");

  // Standard-Felder (id, name, category, url) + Prompt-Felder
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

  // Keywords-Textarea
  c.querySelectorAll("textarea.kw-textarea[data-k='keywords']").forEach(el => {
    el.addEventListener("input", () => {
      const pidx = +el.getAttribute("data-pidx");
      const prod = state.config.products[pidx];
      // Split auf Zeilen ODER Kommas
      prod.keywords = el.value
        .split(/[\n,]/)
        .map(s => s.trim())
        .filter(Boolean);
    });
  });

  // URL-Tracking-Textareas pro Marke
  c.querySelectorAll("textarea.url-textarea[data-brand]").forEach(el => {
    el.addEventListener("input", () => {
      const pidx = +el.getAttribute("data-pidx");
      const brand = el.getAttribute("data-brand");
      const prod = state.config.products[pidx];
      if (!prod.tracked_urls || typeof prod.tracked_urls !== "object") prod.tracked_urls = {};
      const urls = el.value
        .split("\n")
        .map(s => s.trim())
        .filter(Boolean);
      if (urls.length === 0) {
        // leere Liste beibehalten als Signal fuer Auto-Discovery
        prod.tracked_urls[brand] = [];
      } else {
        prod.tracked_urls[brand] = urls;
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

  const url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + apiKey;
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
// Impact-Tab: Website-Events × LLM-Metriken
// ----------------------------------------------------------------------

async function loadCorrelation() {
  const candidates = [
    (state.basePath || "../data/runs/").replace(/runs\/$/, "") + "correlation.json",
    "../data/correlation.json",
    "data/correlation.json",
  ];
  for (const p of candidates) {
    try {
      const r = await fetch(p, { cache: "no-cache" });
      if (r.ok) return await r.json();
    } catch (e) {}
  }
  return null;
}

function fmtDeltaPct(v) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const s = (v * 100);
  const sign = s > 0 ? "+" : "";
  return sign + s.toFixed(1) + " pp";
}

function fmtDeltaRank(v) {
  if (v === null || v === undefined || isNaN(v)) return "–";
  const sign = v > 0 ? "+" : "";
  return sign + v.toFixed(2);
}

function impactDeltaClass(v) {
  if (v === null || v === undefined || isNaN(v)) return "flat";
  if (v > 0.005) return "up";
  if (v < -0.005) return "down";
  return "flat";
}

function renderImpactKpis(data, filtered) {
  const events = filtered || (data && data.events) || [];
  const withImpact = events.filter(e => e.impact_t1 && e.impact_t1.delta);
  const totalEvents = events.length;
  const positive = withImpact.filter(e => (e.impact_t1.delta.delta_share_of_voice || 0) > 0).length;
  const negative = withImpact.filter(e => (e.impact_t1.delta.delta_share_of_voice || 0) < 0).length;
  const bestEvent = withImpact.slice().sort((a, b) =>
    (b.impact_t1.delta.delta_share_of_voice || 0) - (a.impact_t1.delta.delta_share_of_voice || 0)
  )[0];
  const worstEvent = withImpact.slice().sort((a, b) =>
    (a.impact_t1.delta.delta_share_of_voice || 0) - (b.impact_t1.delta.delta_share_of_voice || 0)
  )[0];
  const best = bestEvent ? bestEvent.impact_t1.delta.delta_share_of_voice : null;
  const worst = worstEvent ? worstEvent.impact_t1.delta.delta_share_of_voice : null;

  $("impactKpis").innerHTML = `
    <div class="kpi">
      <div class="label">Events gesamt</div>
      <div class="value">${totalEvents}</div>
      <div class="delta flat">${withImpact.length} mit Impact-Daten</div>
    </div>
    <div class="kpi">
      <div class="label">Positive Events</div>
      <div class="value">${positive}</div>
      <div class="delta up">SoV gestiegen</div>
    </div>
    <div class="kpi">
      <div class="label">Negative Events</div>
      <div class="value">${negative}</div>
      <div class="delta down">SoV gefallen</div>
    </div>
    <div class="kpi">
      <div class="label">Beste Δ SoV</div>
      <div class="value">${fmtDeltaPct(best)}</div>
      <div class="delta ${impactDeltaClass(best)}">${bestEvent ? escapeHtml(bestEvent.brand || "–") : "–"}</div>
    </div>
    <div class="kpi">
      <div class="label">Schlechteste Δ SoV</div>
      <div class="value">${fmtDeltaPct(worst)}</div>
      <div class="delta ${impactDeltaClass(worst)}">${worstEvent ? escapeHtml(worstEvent.brand || "–") : "–"}</div>
    </div>
  `;
}

function renderImpactTimeline(data, runs) {
  // Zeitreihe SoV der eigenen Marke ueber die Runs
  destroyChart("impactTimeline");
  const ownBrand = (state.config && state.config.brand && state.config.brand.name)
    || (runs && runs[0] && runs[0].brand) || "";
  const labels = runs.map(r => (r.run_id || r.finished_at || "").slice(0, 16));
  // SoV-Serie ueber alle Produkte aggregiert
  const sovSeries = runs.map(r => {
    const agg = aggregateRunForBrand(r, ownBrand);
    return agg.share_of_voice !== null && agg.share_of_voice !== undefined
      ? Number((agg.share_of_voice * 100).toFixed(2))
      : null;
  });

  // Event-Marker: Y-Wert = SoV zum naechsten Run nach dem Event
  const events = (data && data.events) || [];
  const eventPoints = [];
  for (const ev of events) {
    const t1 = ev.impact_t1;
    if (!t1 || !t1.t1_run_id) continue;
    const idx = runs.findIndex(r => r.run_id === t1.t1_run_id);
    if (idx < 0) continue;
    eventPoints.push({
      x: labels[idx],
      y: sovSeries[idx],
      eventType: ev.event_type,
      brand: ev.brand,
      dSov: t1.delta && t1.delta.delta_share_of_voice,
    });
  }

  const ctx = document.getElementById("impactTimelineChart");
  if (!ctx) return;
  state.charts.impactTimeline = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "SoV " + ownBrand + " (%)",
          data: sovSeries,
          borderColor: BRAND_COLOR,
          backgroundColor: BRAND_COLOR + "22",
          tension: 0.25,
          fill: true,
          pointRadius: 3,
        },
        {
          type: "scatter",
          label: "Events",
          data: eventPoints.map(p => ({ x: p.x, y: p.y })),
          borderColor: "#f59e0b",
          backgroundColor: "#f59e0b",
          pointRadius: 7,
          pointStyle: "triangle",
          showLine: false,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: "top" },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.dataset.label === "Events") {
                const ev = eventPoints[ctx.dataIndex];
                return [
                  (ev.eventType || "event") + " · " + (ev.brand || "–"),
                  "Δ SoV: " + fmtDeltaPct(ev.dSov),
                ];
              }
              return ctx.dataset.label + ": " + (ctx.parsed.y || 0).toFixed(2) + " %";
            },
          },
        },
      },
      scales: {
        y: { title: { display: true, text: "SoV (%)" }, beginAtZero: true },
      },
    },
  });
}

function aggregateRunForBrand(run, brand) {
  // Simpler Ueberblick: SoV der Marke ueber alle Produkte * LLMs
  const products = run.products || {};
  const llms = run.llms || [];
  let mentions = 0, grand = 0, prompts = 0, appearances = 0, citations = 0;
  let ranks = [];
  for (const pid of Object.keys(products)) {
    const sbl = (products[pid] || {}).summary_by_llm || {};
    for (const llm of llms) {
      const s = sbl[llm] || {};
      const prTot = s.prompts_total || 0;
      for (const row of (s.brands || [])) {
        grand += row.mentions || 0;
        if (row.name === brand) {
          mentions += row.mentions || 0;
          appearances += Math.round((row.appearance_rate || 0) * prTot);
          citations += Math.round((row.citation_rate || 0) * prTot);
          prompts += prTot;
          if (row.avg_rank !== null && row.avg_rank !== undefined) ranks.push(row.avg_rank);
        }
      }
    }
  }
  return {
    share_of_voice: grand ? mentions / grand : null,
    appearance_rate: prompts ? appearances / prompts : null,
    citation_rate: prompts ? citations / prompts : null,
    avg_rank: ranks.length ? ranks.reduce((a, b) => a + b, 0) / ranks.length : null,
  };
}

function renderImpactEventsTable(filtered) {
  const top = filtered.slice().sort((a, b) => {
    const av = a.impact_t1 && a.impact_t1.delta && Math.abs(a.impact_t1.delta.delta_share_of_voice || 0) || 0;
    const bv = b.impact_t1 && b.impact_t1.delta && Math.abs(b.impact_t1.delta.delta_share_of_voice || 0) || 0;
    return bv - av;
  }).slice(0, 50);

  if (!top.length) {
    $("impactEventsTable").innerHTML = '<p class="hint">Noch keine Events mit Impact-Daten (braucht mind. einen Lauf nach dem Event).</p>';
    return;
  }

  const rows = top.map((e, i) => {
    const t1 = e.impact_t1 || {};
    const d = t1.delta || {};
    const cls = impactDeltaClass(d.delta_share_of_voice);
    const pids = (e.product_ids || []).join(", ");
    const when = (e.timestamp || "").slice(0, 16).replace("T", " ");
    const cls_text = e.classification && e.classification.category
      ? ` <span class="pill flat">${escapeHtml(e.classification.category)}</span>`
      : "";
    const summary = (e.summary || "").slice(0, 120);
    return `
      <details class="impact-row" data-idx="${i}">
        <summary>
          <span class="ts">${escapeHtml(when)}</span>
          <span class="brand-pill pill ${e.brand === (state.config && state.config.brand && state.config.brand.name) ? 'brand' : 'comp'}">${escapeHtml(e.brand || "–")}</span>
          <span class="type">${escapeHtml(e.event_type || "–")}</span>
          <span class="pids hint">${escapeHtml(pids)}</span>
          <span class="spacer"></span>
          <span class="delta ${cls}">Δ SoV ${fmtDeltaPct(d.delta_share_of_voice)}</span>
        </summary>
        <div class="impact-detail">
          <div class="grid">
            <div class="subcard">
              <h4>Δ bei t+1 (erster Lauf nach Event)</h4>
              <div class="metric-row"><span>Share of Voice</span><span class="${impactDeltaClass(d.delta_share_of_voice)}">${fmtDeltaPct(d.delta_share_of_voice)}</span></div>
              <div class="metric-row"><span>Appearance Rate</span><span class="${impactDeltaClass(d.delta_appearance_rate)}">${fmtDeltaPct(d.delta_appearance_rate)}</span></div>
              <div class="metric-row"><span>Citation Rate</span><span class="${impactDeltaClass(d.delta_citation_rate)}">${fmtDeltaPct(d.delta_citation_rate)}</span></div>
              <div class="metric-row"><span>Ø Rang (niedriger=besser)</span><span class="${impactDeltaClass(d.delta_avg_rank)}">${fmtDeltaRank(d.delta_avg_rank)}</span></div>
            </div>
            ${e.impact_t2 ? `
            <div class="subcard">
              <h4>Δ bei t+2</h4>
              <div class="metric-row"><span>Share of Voice</span><span class="${impactDeltaClass(e.impact_t2.delta.delta_share_of_voice)}">${fmtDeltaPct(e.impact_t2.delta.delta_share_of_voice)}</span></div>
              <div class="metric-row"><span>Appearance Rate</span><span class="${impactDeltaClass(e.impact_t2.delta.delta_appearance_rate)}">${fmtDeltaPct(e.impact_t2.delta.delta_appearance_rate)}</span></div>
              <div class="metric-row"><span>Citation Rate</span><span class="${impactDeltaClass(e.impact_t2.delta.delta_citation_rate)}">${fmtDeltaPct(e.impact_t2.delta.delta_citation_rate)}</span></div>
              <div class="metric-row"><span>Ø Rang</span><span class="${impactDeltaClass(e.impact_t2.delta.delta_avg_rank)}">${fmtDeltaRank(e.impact_t2.delta.delta_avg_rank)}</span></div>
            </div>` : '<div class="subcard"><h4>Δ bei t+2</h4><p class="hint">noch nicht verfügbar</p></div>'}
          </div>
          <h4 style="margin-top:16px;">Event-Details</h4>
          <div class="metric-row"><span>URL</span><span><a href="${escapeHtml(e.url || "#")}" target="_blank" rel="noopener">${escapeHtml(e.url || "–")}</a></span></div>
          <div class="metric-row"><span>Ähnlichkeit</span><span>${e.similarity !== null && e.similarity !== undefined ? (e.similarity * 100).toFixed(1) + " %" : "–"}</span></div>
          <div class="metric-row"><span>Zeilen +</span><span>${e.added_lines_count || 0}</span></div>
          <div class="metric-row"><span>Zeilen −</span><span>${e.removed_lines_count || 0}</span></div>
          ${e.classification ? `<div class="metric-row"><span>Kategorie</span><span>${escapeHtml(e.classification.category || "–")}${cls_text}</span></div>` : ""}
          ${e.classification && e.classification.reasoning ? `<div class="metric-row"><span>Gemini-Einschätzung</span><span style="max-width:60%">${escapeHtml(e.classification.reasoning)}</span></div>` : ""}
          ${summary ? `<div class="metric-row"><span>Diff-Zusammenfassung</span><span style="max-width:60%">${escapeHtml(summary)}${(e.summary || "").length > 120 ? "…" : ""}</span></div>` : ""}
        </div>
      </details>`;
  }).join("");
  $("impactEventsTable").innerHTML = rows;
}

function populateImpactFilters(data) {
  if (!data) return;
  const brands = new Set();
  const products = new Set();
  for (const e of (data.events || [])) {
    if (e.brand) brands.add(e.brand);
    for (const p of (e.product_ids || [])) products.add(p);
  }
  const bSel = $("impactBrandFilter");
  const pSel = $("impactProductFilter");
  if (bSel) {
    const prev = bSel.value || "all";
    bSel.innerHTML = '<option value="all">Alle Marken</option>' +
      Array.from(brands).sort().map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join("");
    bSel.value = prev;
  }
  if (pSel) {
    const prev = pSel.value || "all";
    pSel.innerHTML = '<option value="all">Alle Produkte</option>' +
      Array.from(products).sort().map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)}</option>`).join("");
    pSel.value = prev;
  }
}

function filterImpactEvents(data) {
  const brand = $("impactBrandFilter") ? $("impactBrandFilter").value : "all";
  const pid = $("impactProductFilter") ? $("impactProductFilter").value : "all";
  const type = $("impactTypeFilter") ? $("impactTypeFilter").value : "all";
  const onlyDelta = $("impactOnlyWithDelta") && $("impactOnlyWithDelta").checked;
  return (data.events || []).filter(e => {
    if (brand !== "all" && e.brand !== brand) return false;
    if (pid !== "all" && !(e.product_ids || []).includes(pid)) return false;
    if (type !== "all" && e.event_type !== type) return false;
    if (onlyDelta) {
      if (!e.impact_t1 || !e.impact_t1.delta) return false;
      const d = e.impact_t1.delta.delta_share_of_voice;
      if (d === null || d === undefined || Math.abs(d) < 0.001) return false;
    }
    return true;
  });
}

async function renderImpactTab() {
  const data = await loadCorrelation();
  if (!data) {
    $("impactEventsTable").innerHTML = '<p class="hint">Keine correlation.json gefunden. Erst einen Lauf durchführen.</p>';
    $("impactKpis").innerHTML = '';
    return;
  }
  // Runs voll laden fuer die Timeline
  const full = await loadLastNRuns(20);
  state.impactData = data;

  populateImpactFilters(data);

  const applyAll = () => {
    const filtered = filterImpactEvents(data);
    renderImpactKpis(data, filtered);
    renderImpactEventsTable(filtered);
    renderImpactTimeline(data, full);
  };
  applyAll();

  ["impactBrandFilter", "impactProductFilter", "impactTypeFilter", "impactOnlyWithDelta"].forEach(id => {
    const el = $(id);
    if (el && !el._impactBound) {
      el.addEventListener("change", applyAll);
      el._impactBound = true;
    }
  });
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
  if (name === "impact") renderImpactTab();
}

async function init() {
  document.querySelectorAll(".tab-btn").forEach(function (b) {
    b.addEventListener("click", function () { switchTab(b.getAttribute("data-tab")); });
  });

  rsStart();

  const idx = await loadIndex();
  if (!idx || !idx.runs.length) {
    $("runMeta").textContent = "Noch keine Laeufe. Starte einen Lauf ueber den Refresh-Button.";
    return;
  }
  state.runs = idx.runs;
  state.basePath = idx.basePath;
  state.selectedRunFile = idx.runs[idx.runs.length - 1].file;
  await loadAndRenderDashboard();

  // Prompt-Manager laden (laufzeitunabhaengig)
  pmLoadAll().catch(e => console.error("pmLoadAll failed:", e));

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



// ----------------------------------------------------------------------
// Prompt-Manager im Dashboard-Tab
// ----------------------------------------------------------------------

async function pmLoadAll() {
  const productOrder = ["zahnzusatz", "risikoleben", "sterbegeld"];
  const bp = state.basePath ? state.basePath.replace(/runs\/$/, "") : "";
  const candidates = (pid) => [
    bp + "prompts/" + pid + ".json",
    "../data/prompts/" + pid + ".json",
    "data/prompts/" + pid + ".json",
  ];
  const out = {};
  for (const pid of productOrder) {
    const res = await tryFetch(candidates(pid));
    if (res) out[pid] = res.data;
  }
  state.pm = { data: out, dirty: new Set(), editing: null };
  state._pmOpen = state._pmOpen || productOrder[0];
  renderPromptManager();
}

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderPromptManager() {
  const el = $("promptManager");
  if (!el || !state.pm) return;
  const data = state.pm.data || {};
  const products = Object.keys(data);
  if (!products.length) {
    el.innerHTML = '<p class="hint">Keine Prompt-Dateien gefunden. Wurde bereits ein Lauf durchgef&uuml;hrt?</p>';
    updatePmSaveBtn();
    return;
  }
  const html = products.map(pid => {
    const entry = data[pid];
    const prompts = entry.prompts || [];
    const rowsHtml = prompts.map((p, idx) => renderPmRow(pid, idx, p)).join("");
    const dirtyMark = state.pm.dirty.has(pid) ? '<span class="pill flat">ge&auml;ndert</span>' : "";
    return `
      <details class="pm-product" ${pid === state._pmOpen ? "open" : ""} data-pid="${escapeAttr(pid)}" onclick="pmOnProductToggle(event, '${escapeAttr(pid)}')">
        <summary>
          <strong>${escapeHtml(entry.product || pid)}</strong>
          <span class="hint">&mdash; ${prompts.length} Prompts</span>
          ${dirtyMark}
        </summary>
        <div class="pm-rows">${rowsHtml}</div>
        <div class="pm-actions">
          <button class="btn-secondary" onclick="pmAddPrompt('${escapeAttr(pid)}')">+ Prompt hinzuf&uuml;gen</button>
        </div>
      </details>`;
  }).join("");
  el.innerHTML = html;
  updatePmSaveBtn();
}

function pmOnProductToggle(evt, pid) {
  // nur den obersten details-Click werten (nicht von inneren Elementen)
  const tgt = evt.target;
  if (tgt && tgt.tagName && tgt.tagName.toLowerCase() !== "summary" && tgt.closest && !tgt.closest("summary")) return;
  // Nachdem Browser state toggled, sync with _pmOpen
  setTimeout(() => {
    const det = document.querySelector(`.pm-product[data-pid="${pid}"]`);
    if (det && det.open) state._pmOpen = pid;
  }, 0);
}

function renderPmRow(pid, idx, p) {
  const isEditing = state.pm.editing &&
    state.pm.editing.pid === pid &&
    state.pm.editing.idx === idx;
  if (isEditing) {
    return `
      <div class="pm-row editing" data-pid="${escapeAttr(pid)}" data-idx="${idx}">
        <input type="text" class="pm-intent" value="${escapeHtml(p.intent || "")}" placeholder="Intent (optional)" />
        <textarea class="pm-text" rows="3" placeholder="Prompt-Text">${escapeHtml(p.text || "")}</textarea>
        <div class="pm-row-actions">
          <button class="btn-primary" onclick="pmSaveRow('${escapeAttr(pid)}', ${idx})">&Uuml;bernehmen</button>
          <button class="btn-secondary" onclick="pmCancelEdit()">Abbrechen</button>
          <button class="btn-danger" onclick="pmDeleteRow('${escapeAttr(pid)}', ${idx})">L&ouml;schen</button>
        </div>
      </div>`;
  }
  return `
    <div class="pm-row" data-pid="${escapeAttr(pid)}" data-idx="${idx}">
      <span class="pm-id">${escapeHtml(p.id || "")}</span>
      <span class="pm-intent-tag">${escapeHtml(p.intent || "")}</span>
      <span class="pm-text-line">${escapeHtml(p.text || "")}</span>
      <button class="btn-icon" title="Bearbeiten" onclick="pmEditRow('${escapeAttr(pid)}', ${idx})">&#9998;</button>
    </div>`;
}

function pmEditRow(pid, idx) {
  state.pm.editing = { pid, idx };
  state._pmOpen = pid;
  renderPromptManager();
}
function pmCancelEdit() {
  const e = state.pm.editing;
  if (e) {
    const row = state.pm.data[e.pid].prompts[e.idx];
    // Wenn neu hinzugef&uuml;gter leerer Row, entfernen
    if (row && !row.text && !row.intent) {
      state.pm.data[e.pid].prompts.splice(e.idx, 1);
      if (state.pm.data[e.pid].prompts.length === 0 ||
          !state.pm.data[e.pid].prompts.some(p => p.text)) {
        // nichts mehr geaendert -> dirty clearen, falls dieses Produkt nur dadurch dirty wurde
        // (konservativ: belassen, damit Nutzer entscheiden kann)
      }
    }
  }
  state.pm.editing = null;
  renderPromptManager();
}
function pmSaveRow(pid, idx) {
  const row = document.querySelector(`.pm-row.editing[data-pid="${pid}"][data-idx="${idx}"]`);
  if (!row) return;
  const intent = row.querySelector(".pm-intent").value.trim();
  const text = row.querySelector(".pm-text").value.trim();
  if (!text) { alert("Prompt-Text darf nicht leer sein."); return; }
  const p = state.pm.data[pid].prompts[idx];
  p.intent = intent;
  p.text = text;
  state.pm.editing = null;
  state.pm.dirty.add(pid);
  state._pmOpen = pid;
  renderPromptManager();
}
function pmDeleteRow(pid, idx) {
  if (!confirm("Diesen Prompt l&ouml;schen?")) return;
  state.pm.data[pid].prompts.splice(idx, 1);
  state.pm.editing = null;
  state.pm.dirty.add(pid);
  state._pmOpen = pid;
  renderPromptManager();
}
function pmAddPrompt(pid) {
  const arr = state.pm.data[pid].prompts;
  const newIdx = arr.length;
  const first = arr[0];
  const prefix = (first && first.id) ? first.id.split("-")[0] : pid.slice(0, 3);
  const maxN = arr.reduce((m, p) => {
    const n = parseInt(((p.id || "").split("-")[1] || "0"), 10);
    return isNaN(n) ? m : Math.max(m, n);
  }, 0);
  const newId = `${prefix}-${String(maxN + 1).padStart(2, "0")}`;
  arr.push({ id: newId, intent: "", text: "" });
  state.pm.editing = { pid, idx: newIdx };
  state._pmOpen = pid;
  state.pm.dirty.add(pid);
  renderPromptManager();
}

function updatePmSaveBtn() {
  const btn = $("pmSaveBtn");
  const status = $("pmStatus");
  if (!btn || !state.pm) return;
  const n = state.pm.dirty.size;
  if (n === 0) {
    btn.disabled = true;
    btn.textContent = "\u00c4nderungen speichern";
    if (status) status.textContent = "Keine \u00c4nderungen.";
  } else {
    btn.disabled = false;
    btn.textContent = "\u00c4nderungen speichern (" + n + ")";
    if (status) status.textContent = n + " Produkt(e) mit \u00c4nderungen.";
  }
}

async function pmSaveAll() {
  const token = localStorage.getItem("gh_token");
  const repo = localStorage.getItem("gh_repo");
  if (!token || !repo) {
    alert("GitHub Token / Repo fehlt. Im Config-Tab nachtragen.");
    return;
  }
  const btn = $("pmSaveBtn");
  btn.disabled = true;
  const originalTxt = btn.textContent;
  btn.textContent = "Speichere \u2026";
  try {
    for (const pid of Array.from(state.pm.dirty)) {
      const content = JSON.stringify(state.pm.data[pid], null, 2);
      const r = await ghPutFile(repo, "data/prompts/" + pid + ".json", content, token, "chore: update prompts " + pid + " via dashboard");
      if (!r.ok && r.status !== 201) throw new Error(pid + ": HTTP " + r.status);
    }
    state.pm.dirty.clear();
    renderPromptManager();
    alert("Gespeichert. Beim n\u00e4chsten Analyse-Lauf werden die neuen Prompts genutzt.");
  } catch (e) {
    alert("Fehler: " + e.message);
    btn.disabled = false;
    btn.textContent = originalTxt;
  }
}




// ----------------------------------------------------------------------
// Run-Status-Badge: zeigt aktuellen Workflow-Lauf in der Topbar
// ----------------------------------------------------------------------

let rsTimer = null;

async function rsPoll() {
  const repo = localStorage.getItem("gh_repo") || "phoeser/geo-visibility-tool";
  if (!repo) return;
  const el = $("runStatus");
  if (!el) return;
  try {
    const url = "https://api.github.com/repos/" + repo + "/actions/workflows/analyze.yml/runs?per_page=1";
    const headers = { "Accept": "application/vnd.github+json" };
    const token = localStorage.getItem("gh_token");
    if (token) headers["Authorization"] = "Bearer " + token;
    const r = await fetch(url, { headers, cache: "no-cache" });
    if (!r.ok) return;
    const d = await r.json();
    const run = (d.workflow_runs || [])[0];
    if (!run) { el.style.display = "none"; return; }
    const active = run.status === "in_progress" || run.status === "queued";
    el.href = run.html_url;
    el.style.display = "inline-flex";
    el.classList.remove("done", "failed");
    if (!active) {
      if (run.conclusion === "success") el.classList.add("done");
      else if (run.conclusion) el.classList.add("failed");
    }
    el.querySelector(".rs-num").textContent = "#" + (run.run_number != null ? run.run_number : "?");
    if (active) {
      const started = run.run_started_at ? new Date(run.run_started_at).toLocaleTimeString() : "";
      el.title = "Lauf #" + run.run_number + " laeuft" + (started ? " (seit " + started + ")" : "");
    } else {
      el.title = "Letzter Lauf #" + run.run_number + ": " + (run.conclusion || "unbekannt");
    }
  } catch (e) {
    // Silent: Netzwerkfehler nicht sichtbar machen
  }
}

function rsStart() {
  rsPoll();
  if (rsTimer) clearInterval(rsTimer);
  // 30 Sek. Polling — ausreichend, nervt die GitHub-API nicht
  rsTimer = setInterval(rsPoll, 30000);
}

init();
