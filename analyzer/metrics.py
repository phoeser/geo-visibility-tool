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
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse


@dataclass
class BrandSpec:
    name: str
    aliases: List[str]
    domain: str
    # 17.07.2026: Weitere Domains derselben Marke (aus config.json "extra_domains").
    # Vorher pruefte cited_brand() NUR die Primaerdomain, waehrend die Aliase in
    # count_mentions() die Zweitmarken laengst mitzaehlten: "DKV" gilt im Text als
    # ERGO-Nennung, ein Zitat von dkv.de galt aber nicht als ERGO-Zitat.
    # Gemessen am Lauf 2026-07-17 (646 Antworten, 6.355 Quelleneintraege):
    #   ERGO        60 -> 62 Zitate (9,3 % -> 9,6 %)  via dkv.de, ergo.com
    #   HUK-Coburg 239 -> 240                          via huk-coburg.de
    #   Allianz    251 -> 251                          (allianzdirect.de nie zitiert)
    # Also ein kleiner, aber systematischer Effekt in eine Richtung - kein Rauschen.
    # ACHTUNG bei Zeitreihen: Aeltere Laeufe tragen gespeicherte Metriken nach alter
    # Logik. Nach dem Deploy tools/backfill_brand_metrics.py ueber alle Runs laufen
    # lassen, sonst entsteht eine Stufe am Umstellungsdatum, die das Treibermodell
    # als Effekt lesen koennte.
    extra_domains: List[str] = field(default_factory=list)


def _build_pattern(aliases: List[str]) -> re.Pattern:
    """Regex, der jede Alias-Variante als ganzes Wort matcht (case-insensitiv)."""
    sorted_aliases = sorted(aliases, key=len, reverse=True)
    escaped = [re.escape(a) for a in sorted_aliases]
    pattern = r"(?<![A-Za-z\xc0-\xff0-9])(" + "|".join(escaped) + r")(?![A-Za-z\xc0-\xff0-9])"
    return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# E1 (26.07.2026): Domain-Aliase und URL-Treffer aus der TEXT-Nennung ausschliessen
# ---------------------------------------------------------------------------
# Zwei getrennte Ursachen, zwei getrennte Filter:
#  1. config.json fuehrt je Marke eine Domain-Form als Alias ("ergo.de", "huk.de").
#     Die zaehlte bisher als Textnennung mit - eine Domain im Fliesstext ist aber
#     ein Quellenverweis, keine Markennennung. _text_aliases() nimmt sie raus.
#  2. Der Alias-Filter allein reicht nicht: der blanke Alias "ergo" matcht per
#     Wortgrenze auch INNERHALB von "www.ergo.de/..." (die Grenze bricht am Punkt).
#     _URL_RE erkennt URLs und blanke Domains; Treffer in solchen Spans werden
#     verworfen.
# Die Zitat-Zuordnung laeuft unveraendert getrennt ueber cited_brand()/domain/
# extra_domains - dieser Filter beruehrt sie nicht.

_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>()\[\]\"']+"
    r"|(?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}(?:/[^\s<>()\[\]\"']*)?",
    re.IGNORECASE,
)


def _text_aliases(aliases: List[str]) -> List[str]:
    """Aliase fuer die TEXT-Nennungszaehlung: Domain-artige Aliase (mit '.')
    fliegen raus. 'ergo.de' ist ein Quellenverweis, keine Markennennung; die
    Domain-Zuordnung fuer Zitate laeuft getrennt ueber cited_brand()."""
    return [a for a in aliases if "." not in a]


def _url_spans(text: str):
    """(start, end)-Spans aller URLs/blanken Domains im Text."""
    return [(m.start(), m.end()) for m in _URL_RE.finditer(text)]


def _pos_in_spans(pos: int, spans) -> bool:
    return any(s <= pos < e for s, e in spans)


# ---------------------------------------------------------------------------
# 1a) Disambiguation: ambige Markennamen vs. gleichlautende Allgemeinwoerter
# ---------------------------------------------------------------------------
# "ergo" (Marke) vs. "ergo" (lat. Adverb = also/folglich).
# Regel seit 17.07.2026 entlang der Gross-/Kleinschreibung - Details in
# _is_ambiguous_false_positive().

_AMBIGUOUS_BRAND_TOKENS = {"ergo"}

# Historisch: Folgewort-Liste der alten Heuristik. Wird seit 17.07.2026 NICHT mehr
# ausgewertet - sie enthielt "ist"/"hat"/"wird" und verwarf damit Saetze wie
# "Ergo ist einer der groessten Versicherer". Bleibt zur Nachvollziehbarkeit stehen.
_CONJUNCTION_FOLLOW_WORDS = {
    "ist", "sind", "war", "waren", "waeren", "waere", "wären", "wäre",
    "kann", "koennen", "können", "konnte", "koennte", "könnte",
    "muss", "muessen", "müssen", "musste", "muesste", "müsste",
    "soll", "sollte", "wird", "wurde", "wuerde", "würde",
    "hat", "haben", "hatte",
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


# Woerter direkt VOR "ergo", die auf die Marke hinweisen (ueberstimmen den Adverb-Check)
_MARKER_PRECEDING_WORDS = {
    # 17.07.2026: "und"/"oder"/"sowie"/"auch"/"wie" ENTFERNT - sie stehen genauso vor
    # dem Adverb ("..., und ergo wird es teurer") und machten es faelschlich zur Marke.
    "empfehle", "empfiehlt", "empfohlen", "empfohlene",
    "nehme", "nimm", "waehle", "wähle", "nutze", "nutzt",
    "bei", "von", "die", "der", "das", "den", "dem",
    "anbieter", "versicherer", "tarif", "tarife",
    "bewertung", "test", "vergleich", "konzern", "marke",
    "wie", "z.b.", "etwa", "beispielsweise",
}


def _is_ambiguous_false_positive(text: str, match_start: int, match_end: int,
                                  matched: str) -> bool:
    """True wenn das Match das Adverb 'ergo' ist statt der Marke.

    NEUFASSUNG 17.07.2026. Die alte Heuristik prueffte Kommas und Folgewoerter und
    loeschte dadurch echte Marken-Nennungen - nachweislich diese:
        "Empfehlenswert sind Allianz, Ergo, AXA"    -> Komma davor  -> verworfen
        "Ergo ist einer der groessten Versicherer"  -> "ist" folgt  -> verworfen
        "Ergo, ein grosser Versicherer, bietet ..." -> Komma danach -> verworfen
    Umgekehrt machte "und"/"oder" in _MARKER_PRECEDING_WORDS aus dem Adverb eine
    Marke ("..., und ergo wird die Police teurer"). Der Filter greift NUR bei ERGO,
    nicht bei Allianz/AXA - Fehler hier verzerren also einseitig.

    Neue Regel entlang dem, was im Deutschen zuverlaessig trennt: Die Marke ist ein
    Eigenname und wird IMMER grossgeschrieben, das lateinische Adverb klein.
        "ERGO"                 -> immer Marke
        "Ergo"                 -> Marke (das Adverb waere hier klein)
        "ergo" in Domain/URL   -> Marke (z.B. www.ergo-reiseversicherung.de)
        "ergo" mit Signalwort  -> Marke ("bei ergo", "Anbieter ergo")
        "ergo" sonst           -> Adverb
    Am Satzanfang sind beide gross und nicht unterscheidbar; dort wird bewusst auf
    Marke entschieden: In Versicherungs-Antworten ist satzinitiales "Ergo" fast immer
    die Marke, und ein verlorener Treffer verzerrt systematisch, ein mitgezaehltes
    Adverb nur zufaellig.

    Getestet: 21/21 Fixtures (alte Fassung 14/21) und gegen 323 echte Antworttexte
    des Laufs 2026-07-16 - dort exakt identisches Ergebnis (192 Nennungen).
    """
    if matched.lower() not in _AMBIGUOUS_BRAND_TOKENS:
        return False
    if matched.isupper():
        return False  # "ERGO"

    # Marken-Signalwort direkt davor -> Marke (auch bei Kleinschreibung)
    before_ctx = text[max(0, match_start - 40):match_start]
    m_pre = re.search(r"([A-Za-z\xc0-\xff\.]+)\W*$", before_ctx)
    if m_pre and m_pre.group(1).lower().rstrip(".") in _MARKER_PRECEDING_WORDS:
        return False

    if matched[:1].isupper():
        return False  # "Ergo" -> Marke

    # Teil einer Domain/URL -> Marke. Die Wortgrenze bricht an "." und "-", das blosse
    # "ergo" matcht deshalb in "www.ergo-reiseversicherung.de" mit.
    ta = match_start
    while ta > 0 and (text[ta - 1].isalnum() or text[ta - 1] in ".-/_"):
        ta -= 1
    tb = match_end
    while tb < len(text) and (text[tb].isalnum() or text[tb] in ".-/_"):
        tb += 1
    token = text[ta:tb]
    if "/" in token or re.search(r"\.[a-z]{2,}", token, re.IGNORECASE):
        return False

    return True  # kleingeschriebenes "ergo" ohne Signal -> Adverb


# ---------------------------------------------------------------------------
# 1) Share of Voice
# ---------------------------------------------------------------------------

def _iter_valid_mentions(text: str, brand: BrandSpec):
    """Liefert die Match-Objekte JEDER Nennung, die count_mentions() zaehlt.

    18.07.2026: Einzige Quelle der Wahrheit fuer "was gilt als Nennung". Sowohl
    count_mentions() (die Zaehlung) als auch mention_contexts() (die Kontext-
    Extraktion fuer die spaetere Empfehlungs-/Sentiment-Klassifikation) laufen
    ueber diesen Generator. Damit ist ausgeschlossen, dass Kontexte nach einer
    anderen Logik gefunden werden als die Zahl, die sie belegen sollen.
    Rein additiv - das Zaehlverhalten ist byte-identisch zur alten Schleife.
    """
    if not text:
        return
    text_aliases = _text_aliases(brand.aliases)
    if not text_aliases:
        return
    pat = _build_pattern(text_aliases)
    url_spans = _url_spans(text)
    for m in pat.finditer(text):
        if _pos_in_spans(m.start(), url_spans):
            continue  # E1: Treffer steckt in einer URL/Domain -> keine Textnennung
        if _is_ambiguous_false_positive(text, m.start(), m.end(), m.group(0)):
            continue
        yield m


def count_mentions(text: str, brand: BrandSpec) -> int:
    return sum(1 for _ in _iter_valid_mentions(text, brand))


def mentioned(text: str, brand: BrandSpec) -> bool:
    return count_mentions(text, brand) > 0


# ---------------------------------------------------------------------------
# 1b) Nennungs-Kontexte (fuer nachtraegliche Empfehlungs-/Sentiment-Analyse)
# ---------------------------------------------------------------------------
# 18.07.2026 (Entscheidung Paul A.2 b): Zusaetzlich zum auf 1.500 Zeichen
# gekuerzten response_text wird je erkannter Markennennung der Satz mit der
# Nennung +/-1 Satz gespeichert - aus dem VOLLEN Antworttext, bevor gekuerzt
# wird. Zweck: echte Empfehlungsrate / Sentiment nachtraeglich klassifizieren
# und Backfills. Gefunden werden die Nennungen ueber _iter_valid_mentions(),
# also mit EXAKT derselben Logik wie die Zaehlung.

_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?]+(?=\s|$)|\n+")


def _sentence_spans(text: str):
    """Pragmatische Satzsegmentierung -> Liste von (start, end)-Spans.

    Grenze: eine .!?-Folge, gefolgt von Whitespace oder Textende, ODER ein
    Block aus Zeilenumbruechen. GRENZEN DES VERFAHRENS: Abkuerzungen mit Punkt
    ("z. B.", "d.h.", "Nr.", "ca.") werden NICHT gesondert behandelt und koennen
    einen Satz zu frueh trennen; Dezimalzahlen wie "4.5" sind durch das
    Whitespace-Lookahead meist geschuetzt. Fuer die grobe Kontext-Ausgabe (Satz
    +/-1) ist das bewusst ausreichend - es geht um Lesbarkeit, nicht um exakte
    Linguistik.
    """
    spans = []
    start = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(text):
        end = m.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text)))
    return spans


def mention_contexts(text: str, brand: BrandSpec,
                     max_contexts: int = 5, max_len: int = 600) -> List[str]:
    """Fuer jede gezaehlte Nennung von `brand`: Satz mit Nennung +/-1 Satz.

    - Nennungen exakt via _iter_valid_mentions() (= Zaehl-Logik).
    - Deduplizierung: mehrere Nennungen im selben Satz ergeben denselben Kontext,
      dieser wird nur einmal ausgegeben.
    - Maximal `max_contexts` (Default 5) verschiedene Kontexte je Marke.
    - Jeder Kontext hart auf `max_len` (Default 600) Zeichen begrenzt; beim
      Abschneiden wird das letzte Zeichen durch "…" ersetzt.
    """
    if not text:
        return []
    spans = _sentence_spans(text)
    if not spans:
        return []
    contexts: List[str] = []
    seen = set()
    for m in _iter_valid_mentions(text, brand):
        pos = m.start()
        idx = None
        for i, (s, e) in enumerate(spans):
            if s <= pos < e:
                idx = i
                break
        if idx is None:
            # Nennung faellt (theoretisch) in einen uebersprungenen Whitespace-
            # Bereich - nimm den naechstgelegenen Satz.
            idx = min(range(len(spans)), key=lambda i: abs(spans[i][0] - pos))
        lo = max(0, idx - 1)
        hi = min(len(spans), idx + 2)
        ctx = text[spans[lo][0]:spans[hi - 1][1]].strip()
        if len(ctx) > max_len:
            ctx = ctx[:max_len - 1].rstrip() + "\u2026"
        if not ctx or ctx in seen:
            continue
        seen.add(ctx)
        contexts.append(ctx)
        if len(contexts) >= max_contexts:
            break
    return contexts


def response_mention_contexts(text: str, brand: BrandSpec,
                              competitors: List[BrandSpec],
                              max_contexts: int = 5,
                              max_len: int = 600) -> Dict[str, List[str]]:
    """mention_contexts fuer Marke + alle Wettbewerber, aus dem VOLLEN Text.

    Nur Marken mit >=1 Kontext landen im Dict - additiv und sparsam.
    Rueckgabe passt direkt ins Antwort-Record als "mention_contexts".
    """
    out: Dict[str, List[str]] = {}
    for b in [brand] + list(competitors or []):
        ctxs = mention_contexts(text, b, max_contexts, max_len)
        if ctxs:
            out[b.name] = ctxs
    return out


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
    text_aliases = _text_aliases(brand.aliases)
    if not text_aliases:
        return None
    pat = _build_pattern(text_aliases)
    items = list(LIST_LINE_RE.finditer(text))
    if not items:
        return None
    for i, match in enumerate(items, start=1):
        body = match.group("body")
        if pat.search(body):
            # Bei ambigem Marken-Token: gleicher Filter wie bei count_mentions
            url_spans = _url_spans(body)
            for sub in pat.finditer(body):
                if _pos_in_spans(sub.start(), url_spans):
                    continue  # E1: Treffer in URL/Domain zaehlt nicht als Rang
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


def _norm_domain(d: str) -> str:
    d = (d or "").lower().strip()
    # Echtes Prefix-Strippen statt lstrip("www."): lstrip nimmt ein Zeichen-SET und
    # haette z.B. "wwk.de" zu "k.de" verstuemmelt.
    if d.startswith("www."):
        d = d[4:]
    return d


def _domain_matches(cited: str, target: str) -> bool:
    """Gehoert die zitierte Domain zur Zielmarke?

    17.07.2026: Vorher "target in cited or cited in target" - blosses Teilstring-
    Matching. Das kann fremde Domains einsammeln:
        target="axa.de"  cited="maxa.de"          -> haette gematcht (fremde Marke)
        target="huk.de"  cited="huk.de.evil.com"  -> haette gematcht (fremde Domain)
    Korrekt ist: exakte Domain oder echte Subdomain.

    EHRLICHKEIT ZUR WIRKUNG: Diese Funktion ist auf den echten Daten ein No-Op.
    Ueber 1.350 verschiedene Domains aller Laeufe ergibt sie exakt dieselben Treffer
    wie die alte Teilstring-Logik - Subdomains traf die naemlich ohnehin, und keines
    der obigen Falsch-Positive kommt real vor. Sie ist reine VORSORGE.
    Der gemessene Effekt (ERGO 60 -> 62 Zitate, HUK-Coburg 239 -> 240) stammt
    vollstaendig aus `extra_domains` unten, nicht aus dieser Funktion.
    """
    c, t = _norm_domain(cited), _norm_domain(target)
    if not c or not t:
        return False
    return c == t or c.endswith("." + t)


def cited_brand(sources: List[Dict[str, str]], brand: BrandSpec) -> bool:
    domains = cited_domains(sources)
    targets = [brand.domain] + list(getattr(brand, "extra_domains", None) or [])
    return any(_domain_matches(d, t) for d in domains if d for t in targets if t)


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

    # 17.07.2026: Fehlgeschlagene Prompts (API-Fehler, leere Antwort) fliegen aus dem
    # Nenner. Vorher zaehlten sie mit: Ein LLM mit HTTP 429 lieferte fuer jede Marke
    # "0 Mentions / nicht genannt", und appearance_rate/citation_rate sanken, ohne dass
    # inhaltlich etwas passiert war - ein toter LLM sah aus wie ein Sichtbarkeitseinbruch.
    # Genau dieses Muster hat am 16.07. den grounded-Kanal auf 0 gesetzt.
    # BEWUSST NOCH NICHT: Quoten auf None setzen, wenn prompts_total==0. main.py:493/514
    # und impact_analysis.py:66 rechnen ungeprueft damit und braechen mit TypeError ab.
    # Der Ausfall ist ueber prompts_total==0 und prompts_error trotzdem erkennbar.
    ok_results = [
        r for r in per_prompt_results
        if not r.get("error") and (r.get("response_text") or "").strip()
    ]
    prompts_error = len(per_prompt_results) - len(ok_results)
    prompts_total = len(ok_results)

    for r in ok_results:
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
        "prompts_error": prompts_error,
        "brands": summary,
    }
