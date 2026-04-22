"""
Impact-Analyse: Vergleicht den aktuellen Lauf mit dem letzten Lauf und
erzeugt (a) quantitative Deltas pro Produkt/Marke/LLM und (b) eine
natürlichsprachliche Executive Summary per Claude.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Helpers zum Navigieren in der Run-Struktur
# ---------------------------------------------------------------------------

def load_run(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def previous_run_file(runs_dir: Path, current_file: Path) -> Optional[Path]:
    runs_dir.mkdir(parents=True, exist_ok=True)
    files = sorted([p for p in runs_dir.glob("*.json") if p.name != current_file.name])
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# Deltas berechnen
# ---------------------------------------------------------------------------

def _brand_lookup(summary: Dict, brand_name: str) -> Optional[Dict]:
    for b in summary.get("brands", []):
        if b["name"] == brand_name:
            return b
    return None


def compute_deltas(current: Dict, previous: Optional[Dict]) -> Dict:
    """Vergleicht aggregierte Metriken pro Produkt/LLM zwischen zwei Läufen."""
    if not previous:
        return {"has_previous": False, "changes": []}

    changes: List[Dict] = []

    # Index der vorigen Summaries
    prev_index: Dict[str, Dict[str, Dict]] = {}
    for prod_id, prod in previous.get("products", {}).items():
        prev_index[prod_id] = prod.get("summary_by_llm", {})

    for prod_id, prod in current.get("products", {}).items():
        prev_llms = prev_index.get(prod_id, {})
        for llm_id, summary in prod.get("summary_by_llm", {}).items():
            prev_summary = prev_llms.get(llm_id)
            if not prev_summary:
                continue
            for brand in summary.get("brands", []):
                prev_brand = _brand_lookup(prev_summary, brand["name"])
                if not prev_brand:
                    continue
                d_sov = brand["share_of_voice"] - prev_brand["share_of_voice"]
                d_app = brand["appearance_rate"] - prev_brand["appearance_rate"]
                d_cit = brand["citation_rate"] - prev_brand["citation_rate"]
                cur_rank = brand.get("avg_rank")
                prev_rank = prev_brand.get("avg_rank")
                d_rank = None
                if cur_rank is not None and prev_rank is not None:
                    # Negatives Delta = Verbesserung (Rang sinkt)
                    d_rank = round(cur_rank - prev_rank, 2)
                changes.append({
                    "product": prod_id,
                    "llm": llm_id,
                    "brand": brand["name"],
                    "delta_share_of_voice": round(d_sov, 4),
                    "delta_appearance_rate": round(d_app, 4),
                    "delta_citation_rate": round(d_cit, 4),
                    "delta_avg_rank": d_rank,
                    "current": {
                        "share_of_voice": brand["share_of_voice"],
                        "appearance_rate": brand["appearance_rate"],
                        "citation_rate": brand["citation_rate"],
                        "avg_rank": brand["avg_rank"],
                    },
                    "previous": {
                        "share_of_voice": prev_brand["share_of_voice"],
                        "appearance_rate": prev_brand["appearance_rate"],
                        "citation_rate": prev_brand["citation_rate"],
                        "avg_rank": prev_brand["avg_rank"],
                    },
                })

    return {
        "has_previous": True,
        "previous_run": previous.get("run_id"),
        "changes": changes,
    }


# ---------------------------------------------------------------------------
# Top-Veränderungen filtern
# ---------------------------------------------------------------------------

def top_changes(deltas: Dict, n: int = 10) -> List[Dict]:
    """Wählt die signifikantesten Änderungen aus (nach |ΔSoV|)."""
    items = deltas.get("changes", [])
    items_sorted = sorted(
        items,
        key=lambda x: abs(x.get("delta_share_of_voice") or 0),
        reverse=True,
    )
    return items_sorted[:n]


# ---------------------------------------------------------------------------
# Executive Summary per LLM
# ---------------------------------------------------------------------------

EXEC_SUMMARY_PROMPT = """Du bist ein Analyst für Marken-Sichtbarkeit in LLMs.
Analysiere die folgenden Daten eines aktuellen Laufs und vergleiche sie
mit dem vorherigen Lauf. Erstelle eine deutschsprachige Executive Summary
in maximal 10 Stichpunkten. Fokus:

- Wo hat die beobachtete Marke gewonnen/verloren? (Share of Voice, Position, Zitierungen)
- Welche Wettbewerber haben sich verschoben?
- Gab es relevante Webseiten-Änderungen bei der beobachteten Marke und könnten
  diese die Sichtbarkeits-Veränderungen erklären?
- Konkrete Handlungsempfehlungen.

Halte dich kurz, knackig, managementtauglich. Keine Wiederholung der Rohzahlen —
nur Interpretation und Einordnung."""


def build_exec_summary_input(
    current: Dict, previous: Optional[Dict], deltas: Dict
) -> str:
    payload = {
        "brand": current.get("brand"),
        "run_id": current.get("run_id"),
        "previous_run_id": previous.get("run_id") if previous else None,
        "top_changes": top_changes(deltas, n=15),
        "website_diffs": {
            pid: prod.get("website", {}).get("diff", {}).get("summary", "")
            for pid, prod in current.get("products", {}).items()
        },
        "website_added_highlights": {
            pid: prod.get("website", {}).get("diff", {}).get("added_lines", [])[:10]
            for pid, prod in current.get("products", {}).items()
        },
        "website_removed_highlights": {
            pid: prod.get("website", {}).get("diff", {}).get("removed_lines", [])[:10]
            for pid, prod in current.get("products", {}).items()
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def generate_exec_summary(current: Dict, previous: Optional[Dict],
                          deltas: Dict, claude_client) -> str:
    """Nutzt den bereits konfigurierten Claude-Client für die Zusammenfassung."""
    if claude_client is None:
        return ("Executive Summary konnte nicht generiert werden — "
                "Claude-Client nicht verfügbar.")
    body = build_exec_summary_input(current, previous, deltas)
    prompt = f"{EXEC_SUMMARY_PROMPT}\n\nDATEN:\n{body}"
    resp = claude_client.ask(prompt)
    if resp.error:
        return f"Summary-Fehler: {resp.error}"
    return resp.text.strip()
