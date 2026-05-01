"""
Metrik-Engine: extrahiert aus jeder LLM-Antwort die drei Kernmetriken:

1. Nennungsrate (Share of Voice) — wie oft wird jede Marke genannt?
2. Position/Rang in Listen       — wird die Marke als 1./2./3. genannt?
3. Quellen-Zitierung              — wird die Marken-Domain als Quelle verlinkt?

Input:  ein LLM-Antworttext + die Marke + Wettbewerber-Config
Output: ein normalisiertes Metrik-Dict pro Antwort
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlparse


@dataclass
class BrandSpec:
    name: str
    aliases: List[str]
    domain: str


def _build_pattern(aliases: List[str]) -> re.Pattern:
    """Regex, der jede Alias-Variante als ganzes Wort matcht (case-insensitiv)."""
    sorted_aliases = sorted(aliases, key=len, reverse=True)
    escaped = [re.escape(a) for a in sorted_aliases]
    # \b funktioniert für ASCII gut; Umlaute sind bei diesen Markennamen kein Problem
    pattern = r"(?<![A-Za-zÀ-ÿ0-9])(" + "|".join(escaped) + r")(?![A-Za-zÀ-ÿ0-9])"
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 1) Share of Voice — reine Nennungszählung
# ---------------------------------------------------------------------------

def count_mentions(text: str, brand: BrandSpec) -> int:
    if not text:
        return 0
    pat = _build_pattern(brand.aliases)
    return len(pat.findall(text))


def mentioned(text: str, brand: BrandSpec) -> bool:
    return count_mentions(text, brand) > 0


# ---------------------------------------------------------------------------
# 2) Position / Rang in Listen
# ---------------------------------------------------------------------------

LIST_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?P<num>\d+)[\.\)]"              # 1. oder 1)
    r"|[-*•–]"                         # Bullet -, *, •, –
    r"|#{1,3}\s"                       # Markdown-Überschrift
    r")\s*(?P<body>.+)$",
    re.MULTILINE,
)


def first_rank(text: str, brand: BrandSpec) -> Optional[int]:
    """Gibt den Rang (1 = ganz oben) zurück, an dem die Marke zum ersten Mal
    in einer nummerierten oder Bullet-Liste erwähnt wird. None = nicht in Liste."""
    if not text:
        return None
    pat = _build_pattern(brand.aliases)
    items = list(LIST_LINE_RE.finditer(text))
    if not items:
        return None

    # Schritte: zuerst sequentielle Bullet-Rangzählung,
    # dann überschreiben durch nummerierte Ränge falls vorhanden.
    for i, match in enumerate(items, start=1):
        body = match.group("body")
        if pat.search(body):
            num = match.group("num")
            return int(num) if num else i
    return None


# ---------------------------------------------------------------------------
# 3) Quellen-Zitierung
# ---------------------------------------------------------------------------

def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:  # noqa: BLE001
        return ""


def cited_domains(sources: List[Dict[str, str]]) -> List[str]:
    return [domain_of(s.get("url", "")) for s in sources if s.get("url")]


def cited_brand(sources: List[Dict[str, str]], brand: BrandSpec) -> bool:
    domains = cited_domains(sources)
    target = brand.domain.lower().lstrip("www.")
    return any(target in d or d in target for d in domains if d)


# ---------------------------------------------------------------------------
# Alles auf einmal
# ---------------------------------------------------------------------------

def compute_per_brand(text: str, sources: List[Dict[str, str]], brand: BrandSpec) -> Dict:
    return {
        "name": brand.name,
        "domain": brand.domain,
        "mentions": count_mentions(text, brand),
        "mentioned": mentioned(text, brand),
        "first_rank": first_rank(text, brand),
        "cited": cited_brand(sources, brand),
    }


def analyse_response(text: str, sources: List[Dict[str, str]],
                     brand: BrandSpec, competitors: List[BrandSpec]) -> Dict:
    """Haupt-Entry-Point: liefert Metriken für Marke + alle Wettbewerber."""
    per_brand = [compute_per_brand(text, sources, brand)]
    for c in competitors:
        per_brand.append(compute_per_brand(text, sources, c))

    total_mentions = sum(b["mentions"] for b in per_brand)
    for b in per_brand:
        b["share_of_voice"] = round(
            b["mentions"] / total_mentions, 4
        ) if total_mentions > 0 else 0.0

    return {
        "brands": per_brand,
        "total_mentions": total_mentions,
        "source_count": len(sources or []),
        "text_length": len(text or ""),
    }


# ---------------------------------------------------------------------------
# Aggregation über alle Prompts
# ---------------------------------------------------------------------------

def aggregate_product_metrics(per_prompt_results: List[Dict],
                              brand_names: List[str]) -> Dict:
    """
    Fasst die pro-Prompt-Metriken für ein Produkt zusammen.
    per_prompt_results: Liste von Dicts {"metrics": {...}, ...}
    """
    totals = {name: {
        "mention_count": 0,
        "appearance_count": 0,   # in wie vielen Prompts überhaupt genannt
        "ranks": [],
        "cited_count": 0,
    } for name in brand_names}

    prompts_total = len(per_prompt_results)
    for r in per_prompt_results:
        m = r.get("metrics", {})
        for b in m.get("brands", []):
            name = b["name"]
            if name not in totals:
                continue
            totals[name]["mention_count"] += b["mentions"]
            if b["mentioned"]:
                totals[name]["appearance_count"] += 1
            if b["first_rank"] is not None:
                totals[name]["ranks"].append(b["first_rank"])
            if b["cited"]:
                totals[name]["cited_count"] += 1

    total_all = sum(totals[n]["mention_count"] for n in totals) or 1
    summary = []
    for name, data in totals.items():
        ranks = data["ranks"]
        summary.append({
            "name": name,
            "mentions": data["mention_count"],
            "share_of_voice": round(data["mention_count"] / total_all, 4),
            "appearance_rate": round(data["appearance_count"] / prompts_total, 4)
                               if prompts_total else 0.0,
            "avg_rank": round(sum(ranks) / len(ranks), 2) if ranks else None,
            "best_rank": min(ranks) if ranks else None,
            "citation_rate": round(data["cited_count"] / prompts_total, 4)
                             if prompts_total else 0.0,
        })

    summary.sort(key=lambda x: x["share_of_voice"], reverse=True)
    return {
        "prompts_total": prompts_total,
        "brands": summary,
    }
