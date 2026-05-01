"""
Korrelations-Engine: verknuepft Webseiten-Events UND Cockpit-Events
mit Metrik-Veraenderungen der LLM-Sichtbarkeit.

Erweiterte Version: Neben den bisherigen Page-Events (change, first_seen)
werden jetzt auch Cockpit-Events (review_change, press_mention, rating_update,
berater_shift, news_mention, etc.) aus shared/events.jsonl eingelesen.

Neu: Lag-Analyse mit konfigurierbaren Zeitfenstern und Spearman-Korrelation
pro Event-Typ x Metrik x Brand.

Workflow:
1. Lade alle Runs (data/runs/*.json), sortiere nach finished_at.
2. Lade Page-Events (data/pages/<brand>/<urlhash>/events.jsonl).
3. Lade Cockpit-Events (shared/events.jsonl oder cockpit_events_file).
4. Fuer jedes Event bestimme Baseline + Lag-Runs (t+1d, t+3d, t+7d, t+14d).
5. Berechne DeltaMetriken pro Lag-Window.
6. Aggregiere: Welcher Event-Typ hat die staerkste Korrelation zu welcher Metrik?
7. Ergebnis: data/correlation.json (erweitert um unified_correlation).

Die Datei wird vom Dashboard-Tab "Korrelation" konsumiert.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Metrik-Aggregation pro Run x Marke x Produkte
# ---------------------------------------------------------------------------

def _aggregate_run(run: dict, brand: str, product_ids: List[str]) -> Dict[str, Optional[float]]:
    """
    Bildet fuer eine Marke und eine Liste von Produkt-IDs die zusammengefasste
    Metrik-Sicht (SoV, appearance_rate, citation_rate, avg_rank, prompts_total).
    """
    products = run.get("products") or {}
    llms: List[str] = run.get("llms") or []
    pids = [p for p in product_ids if p in products]

    mentions = 0
    appearances = 0
    citations = 0
    prompts = 0
    ranks: List[float] = []
    grand_mentions = 0

    for pid in pids:
        p = products.get(pid) or {}
        summary = p.get("summary_by_llm") or {}
        for llm in llms:
            s = summary.get(llm) or {}
            prompts_llm = int(s.get("prompts_total") or 0)
            brand_rows = s.get("brands") or []
            for row in brand_rows:
                grand_mentions += int(row.get("mentions") or 0)
            for row in brand_rows:
                if row.get("name") != brand:
                    continue
                m = int(row.get("mentions") or 0)
                mentions += m
                appearances += round(float(row.get("appearance_rate") or 0) * prompts_llm)
                citations += round(float(row.get("citation_rate") or 0) * prompts_llm)
                prompts += prompts_llm
                if row.get("avg_rank") is not None:
                    try:
                        ranks.append(float(row["avg_rank"]))
                    except Exception:
                        pass

    sov = (mentions / grand_mentions) if grand_mentions else None
    app = (appearances / prompts) if prompts else None
    cit = (citations / prompts) if prompts else None
    rank = (sum(ranks) / len(ranks)) if ranks else None
    return {
        "share_of_voice": sov,
        "appearance_rate": app,
        "citation_rate": cit,
        "avg_rank": rank,
        "mentions": mentions,
        "prompts": prompts,
    }


def _delta(base: Dict, later: Dict) -> Dict:
    def d(key, invert: bool = False):
        a, b = base.get(key), later.get(key)
        if a is None or b is None:
            return None
        diff = b - a
        return -diff if invert else diff
    return {
        "delta_share_of_voice": d("share_of_voice"),
        "delta_appearance_rate": d("appearance_rate"),
        "delta_citation_rate": d("citation_rate"),
        "delta_avg_rank": d("avg_rank", invert=True),
    }


# ---------------------------------------------------------------------------
# Runs + Events laden
# ---------------------------------------------------------------------------

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """
    Akzeptiert ISO-8601 und unser dateinamen-sicheres Format.
    """
    if not s:
        return None
    orig = s
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        pass
    try:
        import re as _re
        s2 = _re.sub(r"(T\d{2})-(\d{2})-(\d{2})", r"\1:\2:\3", orig)
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def load_runs(runs_dir: Path) -> List[dict]:
    runs: List[dict] = []
    for fp in sorted(runs_dir.glob("*.json")):
        if fp.name in ("index.json", "latest.json"):
            continue
        try:
            runs.append(json.loads(fp.read_text(encoding="utf-8")))
        except Exception:
            continue
    if not runs:
        for fp in sorted(runs_dir.glob("*.json")):
            if fp.name in ("index.json", "latest.json"):
                continue
            try:
                runs.append(json.loads(fp.read_text(encoding="utf-8")))
            except Exception:
                continue
    def key(r):
        return _parse_ts(r.get("finished_at") or r.get("started_at") or "") or datetime.min
    runs.sort(key=key)
    return runs


def load_all_events(pages_dir: Path) -> List[dict]:
    """Lade Page-Events (bisheriges Format: change, first_seen)."""
    out: List[dict] = []
    if not pages_dir.exists():
        return out
    for brand_dir in pages_dir.iterdir():
        if not brand_dir.is_dir():
            continue
        for page_dir in brand_dir.iterdir():
            ev = page_dir / "events.jsonl"
            if not ev.exists():
                continue
            try:
                for line in ev.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
            except Exception:
                continue
    out.sort(key=lambda e: e.get("timestamp") or "")
    return out


def load_cockpit_events(events_file: Path) -> List[dict]:
    """Lade Cockpit-Events aus shared/events.jsonl (neues unified Format)."""
    out: List[dict] = []
    if not events_file.exists():
        return out
    try:
        for line in events_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                # Cockpit-Events haben andere event_types als Page-Events
                # Normalisiere: stelle sicher dass brand und timestamp vorhanden
                if ev.get("timestamp") and ev.get("brand"):
                    out.append(ev)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    out.sort(key=lambda e: e.get("timestamp") or "")
    return out


# ---------------------------------------------------------------------------
# Zuordnung Event -> Runs (erweitert mit Lag-Windows)
# ---------------------------------------------------------------------------

def _bracket(runs: List[dict], ts: datetime) -> Tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Liefert (baseline, t1, t2) - unveraendert fuer Abwaertskompatibilitaet."""
    before: List[dict] = []
    after: List[dict] = []
    for r in runs:
        rts = _parse_ts(r.get("finished_at") or r.get("started_at"))
        if not rts:
            continue
        if rts < ts:
            before.append(r)
        else:
            after.append(r)
    baseline = before[-1] if before else None
    t1 = after[0] if after else None
    t2 = after[1] if len(after) > 1 else None
    return baseline, t1, t2


def _find_closest_run(runs: List[dict], target_ts: datetime, after: bool = True) -> Optional[dict]:
    """Finde den Run, der einem Timestamp am naechsten liegt (davor oder danach)."""
    best = None
    best_diff = None
    for r in runs:
        rts = _parse_ts(r.get("finished_at") or r.get("started_at"))
        if not rts:
            continue
        if after and rts < target_ts:
            continue
        if not after and rts > target_ts:
            continue
        diff = abs((rts - target_ts).total_seconds())
        if best_diff is None or diff < best_diff:
            best = r
            best_diff = diff
    return best


def _find_run_in_window(runs: List[dict], ts: datetime, lag_days: int, tolerance_days: int = 2) -> Optional[dict]:
    """Finde den Run, der ca. lag_days Tage nach ts liegt (+-tolerance)."""
    target = ts + timedelta(days=lag_days)
    window_start = target - timedelta(days=tolerance_days)
    window_end = target + timedelta(days=tolerance_days)
    
    best = None
    best_diff = None
    for r in runs:
        rts = _parse_ts(r.get("finished_at") or r.get("started_at"))
        if not rts:
            continue
        if rts < window_start or rts > window_end:
            continue
        diff = abs((rts - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best = r
            best_diff = diff
    return best


# ---------------------------------------------------------------------------
# Spearman-Rang-Korrelation (ohne scipy)
# ---------------------------------------------------------------------------

def _rank(values: List[float]) -> List[float]:
    """Berechne Raenge (1-basiert, Mittelwert bei Gleichstand)."""
    n = len(values)
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n - 1 and indexed[j + 1][1] == indexed[j][1]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def spearman_correlation(x: List[float], y: List[float]) -> Optional[float]:
    """Berechne Spearman-Rangkorrelation. Gibt None zurueck wenn < 3 Punkte."""
    if len(x) != len(y) or len(x) < 3:
        return None
    rx = _rank(x)
    ry = _rank(y)
    n = len(x)
    d_sq_sum = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    denom = n * (n * n - 1)
    if denom == 0:
        return None
    rho = 1 - (6 * d_sq_sum / denom)
    return round(rho, 4)


# ---------------------------------------------------------------------------
# Haupt-Pipeline (Original, abwaertskompatibel)
# ---------------------------------------------------------------------------

def compute(pages_dir: Path, runs_dir: Path) -> Dict:
    """Originale compute()-Funktion — unveraendert fuer Abwaertskompatibilitaet."""
    runs = load_runs(runs_dir)
    events = load_all_events(pages_dir)

    out_events: List[Dict] = []
    for ev in events:
        if ev.get("event_type") not in ("change", "first_seen"):
            continue
        if ev.get("event_type") == "change":
            sim = ev.get("similarity")
            a = ev.get("added_lines_count") or 0
            r = ev.get("removed_lines_count") or 0
            if isinstance(sim, (int, float)) and sim >= 0.97 and (a + r) <= 10:
                continue
        ts = _parse_ts(ev.get("timestamp"))
        if not ts:
            continue

        baseline, t1, t2 = _bracket(runs, ts)
        brand = ev.get("brand") or ""
        pids = ev.get("product_ids") or []

        impact_t1 = None
        impact_t2 = None
        if baseline and t1:
            base_metrics = _aggregate_run(baseline, brand, pids)
            t1_metrics = _aggregate_run(t1, brand, pids)
            impact_t1 = {
                "baseline_run_id": baseline.get("run_id"),
                "t1_run_id": t1.get("run_id"),
                "baseline": base_metrics,
                "t1": t1_metrics,
                "delta": _delta(base_metrics, t1_metrics),
            }
        if baseline and t2:
            base_metrics = _aggregate_run(baseline, brand, pids)
            t2_metrics = _aggregate_run(t2, brand, pids)
            impact_t2 = {
                "baseline_run_id": baseline.get("run_id"),
                "t2_run_id": t2.get("run_id"),
                "baseline": base_metrics,
                "t2": t2_metrics,
                "delta": _delta(base_metrics, t2_metrics),
            }

        out_events.append({
            "timestamp": ev.get("timestamp"),
            "run_id_observed": ev.get("run_id"),
            "brand": brand,
            "product_ids": pids,
            "url": ev.get("url"),
            "event_type": ev.get("event_type"),
            "summary": ev.get("summary"),
            "similarity": ev.get("similarity"),
            "added_lines_count": ev.get("added_lines_count") or 0,
            "removed_lines_count": ev.get("removed_lines_count") or 0,
            "added_lines": [s[:200] for s in (ev.get("added_lines") or [])][:30],
            "removed_lines": [s[:200] for s in (ev.get("removed_lines") or [])][:30],
            "classification": ev.get("classification"),
            "impact_t1": impact_t1,
            "impact_t2": impact_t2,
        })

    def magnitude(e: Dict) -> float:
        t1 = e.get("impact_t1") or {}
        d = (t1.get("delta") or {}).get("delta_share_of_voice")
        return abs(d) if isinstance(d, (int, float)) else -1.0
    top_events = sorted(out_events, key=magnitude, reverse=True)

    return {
        "meta": {
            "total_events": len(out_events),
            "total_runs": len(runs),
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
        "events": out_events,
        "top_events": top_events[:50],
    }


# ---------------------------------------------------------------------------
# NEUE Unified Korrelations-Pipeline
# ---------------------------------------------------------------------------

# Standard-Produkte fuer Brand-Aggregation wenn kein Produkt angegeben
ALL_PRODUCTS = ["zahnzusatz", "sterbegeld", "risikoleben"]

# Brand-Name-Mapping (Cockpit -> GEO)
BRAND_ALIASES = {
    "ERGO": "ERGO",
    "Allianz": "Allianz",
    "AXA": "AXA",
    "Generali": "Generali",
    "HUK-Coburg": "HUK-Coburg",
    "HUK": "HUK-Coburg",
    "Signal Iduna": "Signal Iduna",
    "R+V": "R+V",
    "DEVK": "DEVK",
    "Hannoversche": "Hannoversche",
    "Cosmos Direkt": "Cosmos Direkt",
}

# Cockpit-Event-Typen (im Gegensatz zu Page-Event-Typen change/first_seen)
COCKPIT_EVENT_TYPES = {
    "review_change", "review_volume", "press_mention", "news_mention",
    "rating_update", "berater_shift", "domain_change", "sov_change",
}

# Lag-Windows in Tagen
DEFAULT_LAG_WINDOWS = [1, 3, 7, 14]


def compute_unified(
    pages_dir: Path,
    runs_dir: Path,
    cockpit_events_file: Optional[Path] = None,
    lag_windows: Optional[List[int]] = None,
) -> Dict:
    """
    Erweiterte Korrelation: Page-Events + Cockpit-Events + Lag-Analyse.
    
    Args:
        pages_dir: Pfad zu data/pages/ (Page-Events)
        runs_dir: Pfad zu data/runs/ (GEO-Runs)
        cockpit_events_file: Pfad zu shared/events.jsonl (Cockpit-Events)
        lag_windows: Liste von Lag-Tagen fuer die Analyse [1, 3, 7, 14]
    
    Returns:
        Dict mit: page_correlation (original), unified_events, lag_analysis,
        correlation_matrix, event_summary
    """
    if lag_windows is None:
        lag_windows = DEFAULT_LAG_WINDOWS
    
    runs = load_runs(runs_dir)
    page_events = load_all_events(pages_dir)
    cockpit_events = []
    if cockpit_events_file:
        cockpit_events = load_cockpit_events(cockpit_events_file)
    
    # --- 1. Alle Events in einheitliches Format bringen ---
    unified: List[Dict] = []
    
    # Page-Events normalisieren
    for ev in page_events:
        if ev.get("event_type") not in ("change", "first_seen"):
            continue
        if ev.get("event_type") == "change":
            sim = ev.get("similarity")
            a = ev.get("added_lines_count") or 0
            r = ev.get("removed_lines_count") or 0
            if isinstance(sim, (int, float)) and sim >= 0.97 and (a + r) <= 10:
                continue
        unified.append({
            "timestamp": ev.get("timestamp"),
            "event_type": "page_change" if ev.get("event_type") == "change" else "page_new",
            "brand": ev.get("brand") or "",
            "product": None,
            "product_ids": ev.get("product_ids") or ALL_PRODUCTS,
            "source": "page_tracker",
            "magnitude": 1.0 - (ev.get("similarity") or 0.0) if ev.get("event_type") == "change" else 1.0,
            "detail": {
                "url": ev.get("url"),
                "summary": ev.get("summary"),
                "similarity": ev.get("similarity"),
                "classification": ev.get("classification"),
            },
            "origin": "page_event",
        })
    
    # Cockpit-Events normalisieren
    for ev in cockpit_events:
        brand = BRAND_ALIASES.get(ev.get("brand", ""), ev.get("brand", ""))
        product = ev.get("product")
        product_ids = [product] if product else ALL_PRODUCTS
        unified.append({
            "timestamp": ev.get("timestamp"),
            "event_type": ev.get("event_type", "unknown"),
            "brand": brand,
            "product": product,
            "product_ids": product_ids,
            "source": ev.get("source", ""),
            "magnitude": ev.get("magnitude", 1.0),
            "detail": ev.get("detail", {}),
            "origin": "cockpit_event",
            "sentiment": ev.get("sentiment"),
        })
    
    unified.sort(key=lambda e: e.get("timestamp") or "")
    
    # --- 2. Impact-Berechnung pro Event + Lag-Window ---
    enriched_events: List[Dict] = []
    
    for ev in unified:
        ts = _parse_ts(ev.get("timestamp"))
        if not ts:
            continue
        
        brand = ev["brand"]
        pids = ev.get("product_ids") or ALL_PRODUCTS
        
        # Baseline: letzter Run VOR dem Event
        baseline_run = _find_closest_run(runs, ts, after=False)
        if not baseline_run:
            enriched_events.append({**ev, "impacts": {}})
            continue
        
        base_metrics = _aggregate_run(baseline_run, brand, pids)
        
        impacts = {}
        for lag in lag_windows:
            lag_run = _find_run_in_window(runs, ts, lag)
            if lag_run:
                lag_metrics = _aggregate_run(lag_run, brand, pids)
                delta = _delta(base_metrics, lag_metrics)
                impacts[f"lag_{lag}d"] = {
                    "run_id": lag_run.get("run_id"),
                    "metrics": lag_metrics,
                    "delta": delta,
                }
        
        enriched_events.append({
            **ev,
            "baseline_metrics": base_metrics,
            "impacts": impacts,
        })
    
    # --- 3. Korrelations-Matrix: Event-Typ x Metrik x Lag ---
    correlation_matrix: Dict = {}
    metrics_keys = ["delta_share_of_voice", "delta_appearance_rate", "delta_citation_rate"]
    
    for lag in lag_windows:
        lag_key = f"lag_{lag}d"
        for event_type in set(e["event_type"] for e in enriched_events):
            type_events = [e for e in enriched_events if e["event_type"] == event_type and lag_key in e.get("impacts", {})]
            
            if len(type_events) < 3:
                continue
            
            magnitudes = [e.get("magnitude", 1.0) for e in type_events]
            
            for metric_key in metrics_keys:
                deltas = []
                for e in type_events:
                    d = e["impacts"][lag_key].get("delta", {}).get(metric_key)
                    deltas.append(d if d is not None else 0.0)
                
                # Spearman-Korrelation: magnitude vs. delta
                rho = spearman_correlation(magnitudes, deltas)
                
                # Durchschnittliches Delta
                avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
                
                matrix_key = f"{event_type}__{metric_key}__{lag_key}"
                correlation_matrix[matrix_key] = {
                    "event_type": event_type,
                    "metric": metric_key,
                    "lag_days": lag,
                    "n_events": len(type_events),
                    "spearman_rho": rho,
                    "avg_delta": round(avg_delta, 6),
                    "avg_magnitude": round(sum(magnitudes) / len(magnitudes), 3),
                }
    
    # --- 4. Event-Typ-Summary (fuer Dashboard-Heatmap) ---
    event_summary: Dict = {}
    for event_type in set(e["event_type"] for e in enriched_events):
        type_events = [e for e in enriched_events if e["event_type"] == event_type]
        brands = set(e["brand"] for e in type_events if e.get("brand"))
        
        # Bestes Lag-Window fuer diesen Event-Typ finden
        best_lag = None
        best_rho = -2.0
        for lag in lag_windows:
            key = f"{event_type}__delta_share_of_voice__lag_{lag}d"
            entry = correlation_matrix.get(key)
            if entry and entry["spearman_rho"] is not None:
                if abs(entry["spearman_rho"]) > abs(best_rho):
                    best_rho = entry["spearman_rho"]
                    best_lag = lag
        
        # Impact-Metriken fuer das beste Lag-Window
        impact = {}
        if best_lag is not None:
            for mk in metrics_keys:
                key = f"{event_type}__{mk}__lag_{best_lag}d"
                entry = correlation_matrix.get(key)
                if entry:
                    impact[mk] = {
                        "avg_delta": entry["avg_delta"],
                        "spearman_rho": entry["spearman_rho"],
                        "n": entry["n_events"],
                    }
        
        event_summary[event_type] = {
            "count": len(type_events),
            "brands": sorted(brands),
            "avg_magnitude": round(sum(e.get("magnitude", 0) for e in type_events) / len(type_events), 3) if type_events else 0,
            "best_lag_days": best_lag,
            "best_spearman_rho": best_rho if best_rho > -2.0 else None,
            "impact": impact,
        }
    
    # --- 5. Impact-Ranking: sortiert nach staerkstem absolutem Impact ---
    impact_ranking = sorted(
        event_summary.items(),
        key=lambda kv: abs(kv[1].get("best_spearman_rho") or 0),
        reverse=True,
    )
    
    # --- 6. Fuer Dashboard: letzte 100 Events mit kompaktem Format ---
    dashboard_events = []
    for e in enriched_events[-100:]:
        de = {
            "timestamp": e.get("timestamp"),
            "event_type": e.get("event_type"),
            "brand": e.get("brand"),
            "product": e.get("product"),
            "source": e.get("source"),
            "magnitude": e.get("magnitude"),
            "detail": e.get("detail", {}),
        }
        # Kompakten Impact beifuegen (nur SoV-Delta pro Lag)
        if e.get("impacts"):
            de["sov_deltas"] = {}
            for lag_key, imp in e["impacts"].items():
                dsov = (imp.get("delta") or {}).get("delta_share_of_voice")
                if dsov is not None:
                    de["sov_deltas"][lag_key] = round(dsov, 6)
        if e.get("sentiment"):
            de["sentiment"] = e["sentiment"]
        dashboard_events.append(de)
    
    return {
        "meta": {
            "total_page_events": len(page_events),
            "total_cockpit_events": len(cockpit_events),
            "total_unified_events": len(unified),
            "total_enriched_events": len(enriched_events),
            "total_runs": len(runs),
            "lag_windows": lag_windows,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        },
        "event_summary": event_summary,
        "impact_ranking": [{"event_type": k, **v} for k, v in impact_ranking],
        "correlation_matrix": correlation_matrix,
        "dashboard_events": dashboard_events,
    }


def write_correlation_file(out_path: Path, data: Dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# CLI fuer manuelle Tests
if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="Korrelations-Engine: Page-Events + Cockpit-Events")
    ap.add_argument("--pages", required=True, help="Pfad zu data/pages/")
    ap.add_argument("--runs", required=True, help="Pfad zu data/runs/")
    ap.add_argument("--out", required=True, help="Ausgabe-Pfad fuer correlation.json")
    ap.add_argument("--cockpit-events", default=None, help="Pfad zu shared/events.jsonl")
    ap.add_argument("--lags", default="1,3,7,14", help="Komma-getrennte Lag-Windows in Tagen")
    args = ap.parse_args()
    
    lags = [int(x) for x in args.lags.split(",")]
    cockpit_file = Path(args.cockpit_events) if args.cockpit_events else None
    
    # Original-Korrelation (abwaertskompatibel)
    print("=== Original Page-Event-Korrelation ===")
    page_data = compute(Path(args.pages), Path(args.runs))
    print(f"  Page-Events: {page_data['meta']['total_events']}, Runs: {page_data['meta']['total_runs']}")
    
    # Erweiterte Unified-Korrelation
    print("\n=== Unified Korrelation (Page + Cockpit) ===")
    unified_data = compute_unified(
        Path(args.pages), Path(args.runs),
        cockpit_events_file=cockpit_file,
        lag_windows=lags,
    )
    print(f"  Page-Events: {unified_data['meta']['total_page_events']}")
    print(f"  Cockpit-Events: {unified_data['meta']['total_cockpit_events']}")
    print(f"  Unified: {unified_data['meta']['total_unified_events']}")
    print(f"  Runs: {unified_data['meta']['total_runs']}")
    
    # Zusammenfuehren
    combined = {
        "page_correlation": page_data,
        "unified_correlation": unified_data,
    }
    
    write_correlation_file(Path(args.out), combined)
    print(f"\nWrote {args.out}")
    
    # Impact-Ranking ausgeben
    if unified_data.get("impact_ranking"):
        print("\n=== Impact-Ranking ===")
        for item in unified_data["impact_ranking"][:10]:
            rho = item.get("best_spearman_rho")
            rho_str = f"{rho:+.3f}" if rho is not None else "n/a"
            print(f"  {item['event_type']:20s}  n={item['count']:3d}  rho={rho_str}  lag={item.get('best_lag_days','?')}d")
