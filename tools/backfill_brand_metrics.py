"""
Backfill: Berechnet die Brand-Metriken in ALLEN historischen Runs neu.

Hintergrund: Bis Run vom 24.05.2026 zaehlte die Logik in metrics.py das lateinische
Adverb "ergo" faelschlich als ERGO-Marken-Erwaehnung. Dieses Skript laeuft durch
alle data/runs/*.json und re-computed:
  - per result: metrics.brands[*].mentions / mentioned / share_of_voice
  - per result: metrics.total_mentions
  - per llm:    summary_by_llm.brands[*] (mentions, share_of_voice, appearance_rate)
  - per product: totals (falls vorhanden)

Usage:
  python3 -m tools.backfill_brand_metrics                # alle Runs
  python3 -m tools.backfill_brand_metrics --dry-run      # nur loggen, nichts schreiben
  python3 -m tools.backfill_brand_metrics --since 2026-05-01

Markiert jeden gefixten Run mit _backfilled_at-Timestamp.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Repo-Root in den PYTHONPATH einhaengen
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyzer.metrics import (  # noqa: E402
    BrandSpec, compute_per_brand, aggregate_product_metrics,
)


def load_config(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_brand_specs(cfg: Dict) -> Dict[str, BrandSpec]:
    """Liefert {brand_name: BrandSpec} aus config.json."""
    specs: Dict[str, BrandSpec] = {}
    b = cfg.get("brand") or {}
    own_name = b.get("name")
    if own_name:
        specs[own_name] = BrandSpec(
            name=own_name,
            aliases=b.get("aliases") or [own_name],
            domain=b.get("domain") or "",
        )
    for c in cfg.get("competitors") or []:
        name = c.get("name")
        if not name:
            continue
        specs[name] = BrandSpec(
            name=name,
            aliases=c.get("aliases") or [name],
            domain=c.get("domain") or "",
        )
    return specs


def recompute_one_run(run: Dict, brand_specs: Dict[str, BrandSpec]) -> Dict:
    """
    Recomputed alle Brand-Metriken eines einzelnen Run-JSONs.
    Liefert Statistik: {prompts_processed, brands_changed, deltas: {brand: delta}}.
    """
    stats: Dict = {
        "prompts_processed": 0,
        "brand_mention_deltas": {name: 0 for name in brand_specs.keys()},
    }
    own_brand_name = (run.get("brand") or "")
    own_spec = brand_specs.get(own_brand_name)
    comp_specs = [s for name, s in brand_specs.items() if name != own_brand_name]

    products = run.get("products") or {}
    for pid, prod in products.items():
        per_llm = prod.get("per_llm") or []

        # 1) per result: brand_metrics neu
        for llm_entry in per_llm:
            for r in llm_entry.get("results") or []:
                text = r.get("response_text") or ""
                sources = r.get("sources") or []
                old_metrics = r.get("metrics") or {}
                old_brands = old_metrics.get("brands") or []
                # neue Berechnung
                new_per_brand = []
                if own_spec:
                    new_per_brand.append(compute_per_brand(text, sources, own_spec))
                for s in comp_specs:
                    new_per_brand.append(compute_per_brand(text, sources, s))
                total = sum(b["mentions"] for b in new_per_brand)
                for b in new_per_brand:
                    b["share_of_voice"] = round(
                        b["mentions"] / total, 4
                    ) if total > 0 else 0.0
                # Delta-Tracking
                old_by_name = {b.get("name"): b for b in old_brands}
                for b in new_per_brand:
                    name = b["name"]
                    if name not in stats["brand_mention_deltas"]:
                        stats["brand_mention_deltas"][name] = 0
                    delta = b["mentions"] - (old_by_name.get(name) or {}).get("mentions", 0)
                    stats["brand_mention_deltas"][name] += delta
                # zurueckschreiben
                r["metrics"] = {
                    "brands": new_per_brand,
                    "total_mentions": total,
                    "source_count": len(sources),
                    "text_length": len(text),
                }
                stats["prompts_processed"] += 1

        # 2) summary_by_llm neu
        new_summary = {}
        all_brand_names = list(brand_specs.keys())
        for llm_entry in per_llm:
            llm_id = llm_entry.get("llm") or "?"
            results = llm_entry.get("results") or []
            agg = aggregate_product_metrics(results, all_brand_names)
            # Bestehende cost-Info bewahren falls vorhanden
            old_s = (prod.get("summary_by_llm") or {}).get(llm_id, {}) or {}
            new_summary[llm_id] = {
                **{k: v for k, v in old_s.items() if k not in
                   ("brands", "prompts_total")},  # cost/latency etc. bleiben
                **agg,
            }
        prod["summary_by_llm"] = new_summary

    return stats


def find_runs(runs_dir: Path, since: Optional[str] = None) -> List[Path]:
    files: List[Path] = []
    for p in sorted(runs_dir.glob("*.json")):
        if p.name in ("latest.json", "index.json"):
            continue
        if since:
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
                started = obj.get("started_at") or ""
                if started < since:
                    continue
            except Exception:
                continue
        files.append(p)
    return files


def update_latest_and_index(runs_dir: Path) -> None:
    """Aktualisiert latest.json (= juengster Run) und index.json (Liste)."""
    runs = []
    latest_path = None
    latest_started = ""
    for p in sorted(runs_dir.glob("*.json")):
        if p.name in ("latest.json", "index.json"):
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            started = obj.get("started_at") or ""
            if started > latest_started:
                latest_started = started
                latest_path = p
            # index-eintrag wie in main.py
            products = obj.get("products", {}) or {}
            llms_list = obj.get("llms", []) or []
            prompts_total = 0
            cost_total = 0.0
            brand_mentions = 0
            all_mentions = 0
            brand_name = obj.get("brand")
            for pid, pdata in products.items():
                sbl = (pdata or {}).get("summary_by_llm", {}) or {}
                for llm, s in sbl.items():
                    prompts_total += (s or {}).get("prompts_total", 0)
                    cost_total += (s or {}).get("estimated_cost_usd", 0.0) or 0.0
                    for b in (s or {}).get("brands", []) or []:
                        m = b.get("mentions", 0)
                        all_mentions += m
                        if b.get("name") == brand_name:
                            brand_mentions += m
            sov = (brand_mentions / all_mentions) if all_mentions else 0.0
            dq = obj.get("data_quality") or {}
            me_meta = (obj.get("missing_ergo") or {}).get("_meta") or {}
            runs.append({
                "run_id": obj.get("run_id") or p.stem,
                "file": p.name,
                "started_at": obj.get("started_at"),
                "finished_at": obj.get("finished_at"),
                "brand": brand_name,
                "llms": llms_list,
                "products": list(products.keys()),
                "prompts_total": prompts_total,
                "estimated_cost_usd": round(cost_total, 4),
                "brand_share_of_voice": round(sov, 4),
                "quality_grade": dq.get("grade"),
                "quality_score": dq.get("score"),
                "quality_warnings": (dq.get("warnings") or [])[:3],
                "missing_ergo_followups": me_meta.get("followups_total", 0),
                "missing_ergo_success": me_meta.get("successful", 0),
                "backfilled_at": obj.get("_backfilled_at"),
            })
        except Exception as e:
            print(f"[BACKFILL/INDEX] Fehler bei {p.name}: {e}")
            continue

    (runs_dir / "index.json").write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
                    "count": len(runs), "runs": runs},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if latest_path:
        (runs_dir / "latest.json").write_text(
            latest_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    print(f"[BACKFILL/INDEX] index.json + latest.json aktualisiert ({len(runs)} runs)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill historischer Brand-Metriken")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nur loggen, nichts schreiben")
    ap.add_argument("--since", help="Nur Runs ab diesem ISO-Datum bearbeiten")
    ap.add_argument("--config", default=str(PROJECT_ROOT / "data" / "config.json"))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    brand_specs = build_brand_specs(cfg)
    print(f"[BACKFILL] Marken: {list(brand_specs.keys())}")

    runs_dir = PROJECT_ROOT / "data" / "runs"
    files = find_runs(runs_dir, since=args.since)
    print(f"[BACKFILL] {len(files)} Run-Files zu bearbeiten")

    total_stats: Dict = {"prompts_processed": 0,
                         "brand_mention_deltas": {n: 0 for n in brand_specs}}

    for f in files:
        try:
            run = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[BACKFILL] {f.name}: load-error {e}")
            continue
        stats = recompute_one_run(run, brand_specs)
        for k in stats["brand_mention_deltas"]:
            total_stats["brand_mention_deltas"].setdefault(k, 0)
            total_stats["brand_mention_deltas"][k] += stats["brand_mention_deltas"][k]
        total_stats["prompts_processed"] += stats["prompts_processed"]
        print(f"[BACKFILL] {f.name}: prompts={stats['prompts_processed']}, "
              f"deltas={stats['brand_mention_deltas']}")
        if not args.dry_run:
            run["_backfilled_at"] = datetime.now(timezone.utc).isoformat()
            f.write_text(json.dumps(run, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    if not args.dry_run:
        update_latest_and_index(runs_dir)

    print("\n=== TOTAL ===")
    print(f"Prompts processed: {total_stats['prompts_processed']}")
    print("Brand mention-count deltas (NEW - OLD):")
    for name, delta in total_stats["brand_mention_deltas"].items():
        sign = "+" if delta > 0 else ""
        print(f"  {name:20s} {sign}{delta}")
    print("\nDONE" + (" (dry-run, kein Schreiben)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
