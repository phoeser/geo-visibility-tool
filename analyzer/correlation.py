"""
Korrelations-Engine: verknüpft Webseiten-Events mit Metrik-Veränderungen.

Workflow:
1. Lade alle Runs (`data/runs/*.json`), sortiere nach `finished_at`.
2. Lade alle Page-Events (`data/pages/<brand>/<urlhash>/events.jsonl`).
3. Für jedes Event bestimme:
    - Baseline: den letzten Run *vor* dem Event-Timestamp
    - t+1: den ersten Run *nach* dem Event
    - t+2: den zweiten Run *nach* dem Event (optional)
4. Aggregiere pro Run die Marke×Produkt-Metriken (SoV, appearance, citation,
   rank). Das nutzt dieselbe Logik wie die Dashboard-Aggregation.
5. Berechne ΔMetriken zwischen Baseline und t+1 / t+2 für die betroffene
   Marke × betroffene Produkte.
6. Ergebnis: `data/correlation.json` mit:
      - `events`: Liste aller Events + ihr Δ-Impact (absteigend nach
        absoluter ΔSoV-Magnitude)
      - `meta`: Anzahl Events, Anzahl Runs, letzter Aktualisierungs-TS

Die Datei wird vom Dashboard-Tab „Impact" konsumiert.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Metrik-Aggregation pro Run × Marke × Produkte
# ---------------------------------------------------------------------------

def _aggregate_run(run: dict, brand: str, product_ids: List[str]) -> Dict[str, Optional[float]]:
    """
    Bildet für eine Marke und eine Liste von Produkt-IDs die zusammengefasste
    Metrik-Sicht (SoV, appearance_rate, citation_rate, avg_rank, prompts_total).
    Identisch zur JS-Aggregation im Dashboard.
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
        # rank: niedriger ist besser → wir invertieren, damit positiv = Verbesserung
        "delta_avg_rank": d("avg_rank", invert=True),
    }


# ---------------------------------------------------------------------------
# Runs + Events laden
# ---------------------------------------------------------------------------

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """
    Akzeptiert sowohl echtes ISO-8601 ("2026-04-23T16:14:20Z") als auch
    unser dateinamen-sicheres Format mit Bindestrichen in der Uhrzeit
    ("2026-04-23T16-14-20Z"). Beides wird auf datetime mit UTC-Offset gebracht.
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
    # Fallback: Bindestriche in der Uhrzeit durch Doppelpunkte ersetzen
    try:
        import re as _re
        # Pattern: "YYYY-MM-DDTHH-MM-SS" -> "YYYY-MM-DDTHH:MM:SS"
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
    # Fallback: falls keine run_*.json, nimm alle *.json außer index/latest
    if not runs:
        for fp in sorted(runs_dir.glob("*.json")):
            if fp.name in ("index.json", "latest.json"):
                continue
            try:
                runs.append(json.loads(fp.read_text(encoding="utf-8")))
            except Exception:
                continue
    # Sortiere nach finished_at (Fallback started_at)
    def key(r):
        return _parse_ts(r.get("finished_at") or r.get("started_at") or "") or datetime.min
    runs.sort(key=key)
    return runs


def load_all_events(pages_dir: Path) -> List[dict]:
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
    # Sortieren nach timestamp
    out.sort(key=lambda e: e.get("timestamp") or "")
    return out


# ---------------------------------------------------------------------------
# Zuordnung Event → Runs
# ---------------------------------------------------------------------------

def _bracket(runs: List[dict], ts: datetime) -> Tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """
    Liefert (baseline, t1, t2):
      baseline = letzter Run mit finished_at < ts (Stand VOR dem Event)
      t1 = erster Run mit finished_at >= ts
      t2 = zweiter Run mit finished_at >= ts (falls vorhanden)
    """
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


# ---------------------------------------------------------------------------
# Haupt-Pipeline
# ---------------------------------------------------------------------------

def compute(pages_dir: Path, runs_dir: Path) -> Dict:
    runs = load_runs(runs_dir)
    events = load_all_events(pages_dir)

    out_events: List[Dict] = []
    for ev in events:
        if ev.get("event_type") not in ("change", "first_seen"):
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
            "classification": ev.get("classification"),
            "impact_t1": impact_t1,
            "impact_t2": impact_t2,
        })

    # Sortieren nach absoluter SoV-Delta bei t1 (am nützlichsten für Top-Liste)
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
        "events": out_events,             # Chronologisch
        "top_events": top_events[:50],    # Vorsortiert für Dashboard
    }


def write_correlation_file(out_path: Path, data: Dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# CLI für manuelle Tests
if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", required=True)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data = compute(Path(args.pages), Path(args.runs))
    write_correlation_file(Path(args.out), data)
    print(f"Wrote {args.out}  events={data['meta']['total_events']}  runs={data['meta']['total_runs']}")
