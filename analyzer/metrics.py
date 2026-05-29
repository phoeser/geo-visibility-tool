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
    pattern = r"(?<![A-Za-z\xc0-\xff0-9])(" + "|".join(escaped) + r")(?![A-Za-z\xc0-\xff0-9])"
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# 1a) Disambiguation: ambige Markennamen vs. gleichlautende Allgemeinwoerter
# ---------------------------------------------------------------------------
# - "ergo" (Marke) vs. "ergo" (lat. Adverb = also/folglich)
# Heuristik:
#   - ALL-CAPS "ERGO" -> immer Marke
#   - Multi-Word/Domain ("ERGO Direkt", "ergo.de") -> immer Marke
#   - Stand-alone "ergo"/"Ergo" -> Marke NUR wenn kein Adverb-Kontext erkennbar
#     (Komma vor/nach oder typisches Konjunktions-Folgewort)

_AMBIGUOUS_BRAND_TOKENS = {"ergo"}

_CONJUNCTION_FOLLOW_WORDS = {
    "ist", "sind", "war", "waren", "waeren", "waere", "wären", "wäre",
    "kann", "koennen", "können", "konnte", "koennte", "könnte",
    "muss", "muessen", "müssen", "musste", "muesste", "müsste",
    "soll", "sollte", "wird", "wurde", "wuerde", "würde",
    "hat", "haben", "hatte",
    "wuerde", "würde", "wuerden", "würden",
    "zeigt", "spricht", "ergibt", "folgt", "macht",
    "abraten", "empfehle", "raten",
    "geht", "lohnt", "lohnen", "lohnte",
    "nicht", "kein", "keine", "keinen", "keiner",
    "auch", "schon", "noch", "eher",
    "viele", "wenige", "wenig", "viel",
    "eine", "einer", "einen", "ein",
    "es", "er", "sie", "wir", "du", "ich", "man",
    "diese", "dieses", "diesen", "dieser",
    "im", "in", "auf", "bei", "mit", "von", "zu", "fuer", "für",
    "doch", "deshalb", "daher", "also", "folglich", "somit",
    "faellt", "fällt", "bleibt", "passt", "gilt",
    "dass", "ob", "wenn", "weil", "obwohl", "damit",
    "ueber", "über", "unter",
}


# Wörter direkt VOR "ergo" die stark auf Marke hinweisen (überstimmen Adverb-Check)
_MARKER_PRECEDING_WORDS = {
    "und", "oder", "sowie", "auch", "wie",
    "empfehle", "empfiehlt", "empfohlen", "empfohlene",
    "nehme", "nimm", "waehle", "wähle", "nutze", "nutzt",
    "bei", "von", "die", "der", "das", "den", "dem",
    "anbieter", "versicherer", "tarif", "tarife",
    "bewertung", "test", "vergleich", "konzern", "marke",
    "wie", "z.b.", "etwa", "beispielsweise",
}


def _is_ambiguous_false_positive(text: str, match_start: int, match_end: int,
                                  matched: str) -> bool:
    """True wenn das Match das Adverb 'ergo' ist statt der Marke."""
    if matched.lower() not in _AMBIGUOUS_BRAND_TOKENS:
        return False
    if matched.isupper():
        return False

    # 0) Marken-Indikator-Wort direkt davor -> Marke (überstimmt alle Adverb-Checks)
    before_ctx = text[max(0, match_start - 40):match_start]
    m_pre = re.search(r"([A-Za-z\xc0-\xff\.]+)\W*$", before_ctx)
    if m_pre:
        prev_word = m_pre.group(1).lower().rstrip(".")
        if prev_word in _MARKER_PRECEDING_WORDS:
            return False  # ist Marke, kein False-Positive

    # 1) Komma direkt davor -> Konjunktion
    before = text[max(0, match_start - 5):match_start]
    if before.rstrip().endswith(","):
        return True

    # 2) Komma/Punkt direkt danach -> Konjunktion
    after_raw = text[match_end:match_end + 2]
    nxt = after_raw.lstrip()[:1]
    if nxt in (",", "."):
        return True

    # 3) Folgewort pruefen
    after_window = text[match_end:match_end + 60]
    m_word = re.match(r"\s*([A-Za-z\xc0-\xff]+)", after_window)
    if m_word:
        next_word = m_word.group(1).lower()
        if next_word in _CONJUNCTION_FOLLOW_WORDS:
            return True

    return False


# ---------------------------------------------------------------------------
# 1) Share of Voice
# ---------------------------------------------------------------------------

def count_mentions(text: str, brand: BrandSpec) -> int:
    if not text:
        return 0
    pat = _build_pattern(brand.aliases)
    valid = 0
    for m in pat.finditer(text):
        if _is_ambiguous_false_positive(text, m.start(), m.end(), m.group(0)):
            continue
        valid += 1
    return valid


def mentioned(text: str, brand: BrandSpec) -> bool:
    return count_mentions(text, brand) > 0


# ---------------------------------------------------------------------------
# 2) Position / Rang in Listen
# ---------------------------------------------------------------------------

LIST_LINE_RE = re.compile(
    r"^\s*(?:"
    r"(?P<num>\d+)[\.\)]"
    r"|[-*•–]"
    r"|#{1,3}\s"
    r")\s*(?P<body>.+)$",
    re.MULTILINE,
)


def first_rank(text: str, brand: BrandSpec) -> Optional[int]:
    if not text:
        return None
    pat = _build_pattern(brand.aliases)
    items = list(LIST_LINE_RE.finditer(text))
    if not items:
        return None
    for i, match in enumerate(items, start=1):
        body = match.group("body")
        if pat.search(body):
            # Bei ambigem Marken-Token: gleicher Filter wie bei count_mentions
            for sub in pat.finditer(body):
                if _is_ambiguous_false_positive(body, sub.start(), sub.end(), sub.group(0)):
                    continue
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
    except Exception:
        return ""


def cited_domains(sources: List[Dict[str, str]]) -> List[str]:
    return [domain_of(s.get("url", "")) for s in sources if s.get("url")]


def cited_brand(sources: List[Dict[str, str]], brand: BrandSpec) -> bool:
    domains = cited_domains(sources)
    target = brand.domain.lower().lstrip("www.")
    return any(target in d or d in target for d in domains if d)


# ---------------------------------------------------------------------------
# Aggregation
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


def aggregate_product_metrics(per_prompt_results: List[Dict],
                              brand_names: List[str]) -> Dict:
    totals = {name: {
        "mention_count": 0,
        "appearance_count": 0,
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
