"""
Orchestrierung des kompletten Analyse-Laufs.

Aufruf:
    python -m analyzer.main                (nutzt data/config.json)
    python -m analyzer.main --dry-run      (simulierte Antworten, keine API-Calls)
    python -m analyzer.main --limit 3      (nur die ersten 3 Prompts pro Produkt)

Ergebnisse werden in data/runs/<YYYY-MM-DDTHH-MM-SSZ>.json abgelegt und
der neueste Lauf zusätzlich nach data/runs/latest.json kopiert (für das
Dashboard).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Projekt-Root zum Pfad hinzufügen, damit Module immer findbar sind
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer.llm_clients import build_clients, LLMResponse  # noqa: E402
from analyzer.metrics import (  # noqa: E402
    BrandSpec, analyse_response, aggregate_product_metrics,
)
from analyzer.web_scraper import scrape_product  # noqa: E402
from analyzer.impact_analysis import (  # noqa: E402
    load_run, previous_run_file, compute_deltas, generate_exec_summary,
)
from analyzer.page_tracker import track_all as track_all_pages  # noqa: E402
from analyzer.diff_classifier import make_classifier  # noqa: E402
from analyzer import correlation  # noqa: E402
from analyzer import why_analysis  # noqa: E402
from analyzer.sitemap_discovery import discover_for_product  # noqa: E402


DATA_DIR = PROJECT_ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
PAGES_DIR = DATA_DIR / "pages"


# ---------------------------------------------------------------------------
# Config laden
# ---------------------------------------------------------------------------

def load_config() -> Dict:
    cfg_path = DATA_DIR / "config.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def load_prompts(prompts_file: str) -> List[Dict]:
    path = DATA_DIR / prompts_file
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("prompts", [])


# ---------------------------------------------------------------------------
# Dry-run Dummy-Client
# ---------------------------------------------------------------------------

class DummyClient:
    """Erzeugt deterministische Fake-Antworten — für lokale Tests ohne API-Keys."""

    def __init__(self, model: str = "dummy"):
        self.model = model

    def ask(self, prompt: str) -> LLMResponse:
        time.sleep(0.05)
        # deterministisch seedbar
        h = abs(hash(prompt)) % 10
        brands = ["Allianz", "ERGO", "AXA", "Generali", "HUK-Coburg", "DKV"]
        # Reihenfolge rotieren
        shuffled = brands[h:] + brands[:h]
        lines = [
            "Hier sind einige empfehlenswerte Anbieter:",
            *[f"{i+1}. {b} — gute Tarife und solide Leistungen." for i, b in enumerate(shuffled[:5])],
            "",
            "Quellen:",
            "https://www.allianz.de",
            "https://www.ergo.de",
            "https://www.axa.de",
        ]
        text = "\n".join(lines)
        return LLMResponse(
            text=text,
            sources=[{"title": "", "url": u} for u in [
                "https://www.allianz.de", "https://www.ergo.de", "https://www.axa.de",
            ]],
            model=self.model,
            latency_ms=50.0,
            tokens_in=100,
            tokens_out=80,
        )


# ---------------------------------------------------------------------------
# Haupt-Pipeline
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, limit: Optional[int] = None) -> Path:
    cfg = load_config()
    brand_cfg = cfg["brand"]
    brand = BrandSpec(
        name=brand_cfg["name"], aliases=brand_cfg["aliases"],
        domain=brand_cfg["domain"],
    )
    competitors = [
        BrandSpec(name=c["name"], aliases=c["aliases"], domain=c["domain"])
        for c in cfg["competitors"]
    ]
    all_brand_names = [brand.name] + [c.name for c in competitors]

    # LLM-Clients
    if dry_run:
        clients = {
            llm["id"]: DummyClient(model=llm["model"])
            for llm in cfg["llms"] if llm.get("enabled")
        }
    else:
        clients = build_clients(cfg["llms"])

    if not clients:
        print("[FEHLER] Keine LLM-Clients aktiv. Setze API-Keys oder nutze --dry-run.")
        sys.exit(1)

    print(f"[INFO] Aktive LLMs: {list(clients.keys())}")

    # Timestamp für diesen Lauf
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    run_dict: Dict = {
        "run_id": ts,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "brand": brand_cfg["name"],
        "brand_domain": brand_cfg["domain"],
        "competitors": [c.name for c in competitors],
        "llms": list(clients.keys()),
        "products": {},
        "totals": {},
    }

    parallelism = int(cfg.get("settings", {}).get("parallel_requests", 5))

    for product in cfg["products"]:
        pid = product["id"]
        pname = product["name"]
        print(f"\n[PRODUKT] {pname} ({pid})")

        # --- 1) Webseiten-Snapshot ---
        print("  [WEB] Hole Seite ...")
        web_result = scrape_product(
            SNAPSHOTS_DIR, pid, product["url"], ts,
        ) if not dry_run else {
            "product_id": pid, "url": product["url"], "timestamp": ts,
            "status": 200, "error": None,
            "html_hash": "dry", "text_hash": "dry", "text_length": 0,
            "diff": {"has_previous": False, "changed": False,
                     "summary": "dry-run, kein Scrape", "added_lines": [],
                     "removed_lines": [], "similarity": 1.0},
        }
        if web_result.get("error"):
            print(f"  [WEB] Fehler: {web_result['error']}")
        else:
            print(f"  [WEB] OK — {web_result['text_length']} Zeichen Text")
            print(f"  [DIFF] {web_result['diff']['summary']}")

        # --- 2) Prompts laden ---
        prompts = load_prompts(product["prompts_file"])
        if limit:
            prompts = prompts[:limit]
        print(f"  [PROMPTS] {len(prompts)} Stück")

        # --- 3) Alle Prompts an alle LLMs schicken (parallel) ---
        per_prompt_results: List[Dict] = []
        summary_by_llm: Dict[str, Dict] = {}

        tasks = []
        for p in prompts:
            for llm_id, client in clients.items():
                tasks.append((p, llm_id, client))

        raw_by_key: Dict[str, Dict] = {}
        with ThreadPoolExecutor(max_workers=parallelism) as pool:
            futures = {
                pool.submit(_ask_wrapper, p, llm_id, client): (p, llm_id)
                for p, llm_id, client in tasks
            }
            for i, fut in enumerate(as_completed(futures), start=1):
                p, llm_id = futures[fut]
                try:
                    resp = fut.result()
                except Exception as e:  # noqa: BLE001
                    resp = LLMResponse(text="", sources=[], model="?",
                                       latency_ms=0, error=str(e)[:500])
                raw_by_key[f"{llm_id}::{p['id']}"] = {
                    "prompt": p, "response": resp.to_dict(),
                }
                if i % 10 == 0 or i == len(tasks):
                    print(f"    [LLM] {i}/{len(tasks)} abgeschlossen")

        # --- 4) Metriken berechnen ---
        for llm_id in clients.keys():
            results_for_llm = []
            for p in prompts:
                entry = raw_by_key.get(f"{llm_id}::{p['id']}")
                if not entry:
                    continue
                resp = entry["response"]
                metrics = analyse_response(
                    resp["text"], resp["sources"], brand, competitors,
                )
                results_for_llm.append({
                    "prompt_id": p["id"],
                    "prompt_text": p["text"],
                    "intent": p.get("intent"),
                    "response_text": resp["text"],
                    "sources": resp["sources"],
                    "error": resp.get("error"),
                    "latency_ms": resp.get("latency_ms"),
                    "tokens_in": resp.get("tokens_in"),
                    "tokens_out": resp.get("tokens_out"),
                    "metrics": metrics,
                })
            summary_by_llm[llm_id] = aggregate_product_metrics(
                results_for_llm, all_brand_names,
            )
            per_prompt_results.append({
                "llm": llm_id,
                "results": results_for_llm,
            })

        run_dict["products"][pid] = {
            "name": pname,
            "url": product["url"],
            "website": web_result,
            "per_llm": per_prompt_results,
            "summary_by_llm": summary_by_llm,
        }

    # --- 4b) Page-Tracking (eigene Marke + Wettbewerber) ---
    print("\n[PAGES] Tracke konfigurierte URLs pro Marke ...")
    brand_urls = _build_brand_urls(cfg)
    n_urls = sum(len(v) for v in brand_urls.values())
    print(f"[PAGES] {n_urls} URLs über {len(brand_urls)} Marken")
    if dry_run or n_urls == 0:
        page_events: List[Dict] = []
        if dry_run:
            print("[PAGES] dry-run: übersprungen")
    else:
        classifier = make_classifier()  # nutzt GOOGLE_API_KEY, None wenn fehlt
        try:
            page_events = track_all_pages(
                PAGES_DIR,
                timestamp=ts, run_id=ts,
                brand_urls=brand_urls,
                classifier=classifier,
            )
            n_changed = sum(1 for e in page_events if e.get("changed"))
            n_first = sum(1 for e in page_events if e.get("first_seen"))
            print(f"[PAGES] fertig — {n_changed} geändert, {n_first} erstmalig, "
                  f"{len(page_events)-n_changed-n_first} unverändert")
        except Exception as e:  # noqa: BLE001
            print(f"[PAGES] Fehler: {e}")
            page_events = []
    run_dict["page_tracking"] = {
        "brand_urls": brand_urls,
        "events_this_run": page_events,
    }

    # --- 5) Impact-Analyse ---
    print("\n[IMPACT] Vergleich mit vorherigem Lauf ...")
    current_file = RUNS_DIR / f"{ts}.json"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    prev_path = previous_run_file(RUNS_DIR, current_file)
    prev_run = load_run(prev_path) if prev_path else None
    deltas = compute_deltas(run_dict, prev_run)
    run_dict["impact"] = {"deltas": deltas}

    # Executive Summary per Claude (falls verfügbar)
    claude_client = clients.get("claude")
    print("[IMPACT] Erzeuge Executive Summary ...")
    summary_text = generate_exec_summary(run_dict, prev_run, deltas, claude_client)
    run_dict["impact"]["executive_summary"] = summary_text

    # Totals: ein simples Marken-Ranking über alle Produkte/LLMs
    run_dict["totals"] = _compute_totals(run_dict, all_brand_names)

    run_dict["finished_at"] = datetime.now(timezone.utc).isoformat()

    # --- 6) Speichern ---
    current_file.write_text(
        json.dumps(run_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest_file = RUNS_DIR / "latest.json"
    latest_file.write_text(
        json.dumps(run_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # --- 6b) Warum-Analyse: pro (Produkt, Marke) Erklaerung der Sichtbarkeit ---
    why_llm_id = cfg.get("why_analysis_llm") or "claude"
    print(f"\n[WHY] Analysiere Sichtbarkeits-Muster pro Marke (LLM: {why_llm_id}) ...")
    try:
        why_client = clients.get(why_llm_id)
        if not why_client:
            # Client ad-hoc erzeugen, falls LLM im Hauptlauf deaktiviert ist
            llm_cfg = next((l for l in cfg.get("llms", []) if l.get("id") == why_llm_id), None)
            if llm_cfg:
                why_client = build_clients([{**llm_cfg, "enabled": True}]).get(why_llm_id)
        if why_client:
            run_dict["why_analysis"] = why_analysis.analyze_run(run_dict, why_client)
            run_dict["why_analysis_meta"] = {"llm": why_llm_id}
            print(f"[WHY] fertig fuer {len(run_dict['why_analysis'])} Produkte (LLM: {why_llm_id})")
        else:
            print(f"[WHY] uebersprungen (kein {why_llm_id}-Client verfuegbar, API-Key fehlt?)")
            run_dict["why_analysis"] = {}
    except Exception as e:
        print(f"[WHY] Fehler: {e}")
        run_dict["why_analysis"] = {"error": str(e)[:200]}
    # run-file ueberschreiben mit aktualisierten Daten
    current_file.write_text(json.dumps(run_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_file.write_text(json.dumps(run_dict, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 7) Korrelation: Webseiten-Events ↔ Metrik-Veränderungen ---
    print("\n[CORR] Berechne Korrelation Webseiten-Events ↔ Metriken ...")
    try:
        corr = correlation.compute(PAGES_DIR, RUNS_DIR)
        correlation.write_correlation_file(DATA_DIR / "correlation.json", corr)
        print(f"[CORR] events={corr['meta']['total_events']}, "
              f"runs={corr['meta']['total_runs']}")
    except Exception as e:  # noqa: BLE001
        print(f"[CORR] Fehler: {e}")

    # Ein schlankes Index-File für das Dashboard
    _update_index(RUNS_DIR)

    print(f"\n[OK] Lauf abgeschlossen: {current_file.name}")
    print(f"[OK] Dashboard liest: {latest_file.name}")
    return current_file


def _ask_wrapper(prompt: Dict, llm_id: str, client) -> LLMResponse:
    try:
        return client.ask(prompt["text"])
    except Exception as e:  # noqa: BLE001
        return LLMResponse(text="", sources=[], model="?", latency_ms=0,
                           error=f"{llm_id}: {e}")


def _compute_totals(run_dict: Dict, brand_names: List[str]) -> Dict:
    """Aggregiert Gesamtmetriken über alle Produkte × alle LLMs."""
    totals = {name: {"mentions": 0, "appearances": 0, "prompts": 0,
                     "citations": 0, "ranks": []} for name in brand_names}
    for prod in run_dict["products"].values():
        for llm_id, summary in prod.get("summary_by_llm", {}).items():
            prompts_total = summary.get("prompts_total", 0)
            for b in summary.get("brands", []):
                name = b["name"]
                if name not in totals:
                    continue
                totals[name]["prompts"] += prompts_total
                totals[name]["mentions"] += b["mentions"]
                totals[name]["appearances"] += int(
                    round(b["appearance_rate"] * prompts_total)
                )
                totals[name]["citations"] += int(
                    round(b["citation_rate"] * prompts_total)
                )
                if b["avg_rank"] is not None:
                    totals[name]["ranks"].append(b["avg_rank"])
    grand_mentions = sum(t["mentions"] for t in totals.values()) or 1
    out = []
    for name, data in totals.items():
        out.append({
            "name": name,
            "mentions": data["mentions"],
            "share_of_voice": round(data["mentions"] / grand_mentions, 4),
            "appearance_rate": round(data["appearances"] / data["prompts"], 4)
                               if data["prompts"] else 0.0,
            "citation_rate": round(data["citations"] / data["prompts"], 4)
                             if data["prompts"] else 0.0,
            "avg_rank": round(sum(data["ranks"]) / len(data["ranks"]), 2)
                        if data["ranks"] else None,
        })
    out.sort(key=lambda x: x["share_of_voice"], reverse=True)
    return {"ranking": out}


def _build_brand_urls(cfg: Dict, *, auto_discover: bool = True, max_per_brand: int | None = None) -> Dict[str, List[Dict]]:
    """
    Flacht die Config-Struktur in ein `{brand: [{url, product_ids}, ...]}` um.

    Unterstützte Config-Formen pro Produkt (`cfg["products"][i]`):
      • "tracked_urls": {"ERGO": ["url1", "url2"], "Allianz": ["url3"]}
          — bevorzugt, pro Marke mehrere URLs
      • "keywords": ["zahnzusatz", "zahnzusatzversicherung"]
          — wenn tracked_urls für eine Marke leer ist, wird per Sitemap-
            Discovery automatisch ermittelt
      • "url": "..."  (Fallback, wird der eigenen Marke zugeordnet)

    Marken mit Domains werden aus cfg["brand"] + cfg["competitors"] gelesen.
    Eine URL die mehrfach (über mehrere Produkte) für dieselbe Marke auftaucht,
    wird zusammengefasst und bekommt alle passenden product_ids.
    """
    own_brand = (cfg.get("brand") or {}).get("name") or ""

    # Marke -> Liste von Domains (primaer + extras)
    brand_domains: Dict[str, List[str]] = {}
    own = cfg.get("brand") or {}
    if own.get("name") and own.get("domain"):
        brand_domains[own["name"]] = [own["domain"]] + list(own.get("extra_domains") or [])
    for c in cfg.get("competitors", []) or []:
        if c.get("name") and c.get("domain"):
            brand_domains[c["name"]] = [c["domain"]] + list(c.get("extra_domains") or [])

    # (brand, url) -> set(product_ids)
    index: Dict[Tuple[str, str], set] = {}

    for product in cfg.get("products", []):
        pid = product.get("id") or ""
        tracked = product.get("tracked_urls") or {}
        keywords = [k for k in (product.get("keywords") or []) if isinstance(k, str) and k.strip()]

        # 1) tracked_urls-Dict auflösen
        tracked_brands_nonempty = set()
        if isinstance(tracked, dict):
            for brand, urls in tracked.items():
                if not brand:
                    continue
                if isinstance(urls, str):
                    urls = [urls]
                urls = [u.strip() for u in (urls or []) if isinstance(u, str) and u.strip()]
                if urls:
                    tracked_brands_nonempty.add(brand)
                for u in urls:
                    key = (brand, u)
                    index.setdefault(key, set()).add(pid)

        # 2) Auto-Discovery via Sitemap + Homepage-Crawl fuer jede Brand-Domain
        if auto_discover and keywords:
            for brand, domains in brand_domains.items():
                if brand in tracked_brands_nonempty:
                    continue
                for domain in domains:
                    try:
                        res = discover_for_product(domain, keywords, max_urls=max_per_brand)
                    except Exception:
                        continue
                    for u in res.get("urls", []):
                        if isinstance(u, str) and u.strip():
                            key = (brand, u.strip())
                            index.setdefault(key, set()).add(pid)

        # 3) Letzter Fallback: wenn weder tracked_urls noch keywords existieren,
        #    verwende das alte product["url"] für die eigene Marke.
        if not tracked and not keywords:
            u = product.get("url")
            if isinstance(u, str) and u.strip() and own_brand:
                key = (own_brand, u.strip())
                index.setdefault(key, set()).add(pid)

    # In Dict umbauen
    out: Dict[str, List[Dict]] = {}
    for (brand, url), pids in index.items():
        out.setdefault(brand, []).append({
            "url": url,
            "product_ids": sorted(x for x in pids if x),
        })
    # pro Brand alphabetisch stabil sortieren
    for brand in out:
        out[brand].sort(key=lambda e: e["url"])
    return out


def _update_index(runs_dir: Path) -> None:
    """Schreibt data/runs/index.json mit Metadaten aller Läufe."""
    runs = []
    for p in sorted(runs_dir.glob("*.json")):
        if p.name in ("latest.json", "index.json"):
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
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
            })
        except Exception as e:  # noqa: BLE001
            print(f"[INDEX] Fehler bei {p.name}: {e}")
            continue
    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(runs),
        "runs": runs,
    }
    (runs_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="GEO Visibility Analyse-Lauf")
    ap.add_argument("--dry-run", action="store_true",
                    help="Simuliere LLM-Antworten, keine echten API-Calls")
    ap.add_argument("--limit", type=int, default=None,
                    help="Nur die ersten N Prompts je Produkt verarbeiten")
    args = ap.parse_args(argv)

    try:
        out = run(dry_run=args.dry_run, limit=args.limit)
        print(f"\n[DONE] {out}")
        return 0
    except KeyboardInterrupt:
        print("\n[ABBRUCH] Benutzer hat Lauf gestoppt.")
        return 130
    except Exception as e:  # noqa: BLE001
        import traceback
        print(f"\n[FEHLER] {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
