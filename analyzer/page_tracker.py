"""
Page-Tracker: mehrstufiges Scraping + Change-History pro URL.

Pro `(brand, url)`-Kombination wird im Repo eine kleine Ordnerstruktur gepflegt:

    data/pages/<brand_slug>/<url_hash>/
        meta.json        # {url, brand, product_ids, first_seen, last_seen}
        current.json     # zuletzt gesehener Textstand + Hash + Status
        events.jsonl     # append-only Änderungs-Historie

Events enthalten Hash-before/after, Added/Removed-Zeilen, Ähnlichkeit, die
Classifier-Ausgabe (Gemini) und die Run-Zuordnung. Dadurch sind alle
notwendigen Informationen für die spätere Korrelations-Analyse komplett im
Git-Repo nachvollziehbar, ohne dass wir volle HTML-Snapshots jedes einzelnen
Runs aufbewahren müssen.

Das Modul:
 - respektiert robots.txt (per Marke einmalig abrufen)
 - drosselt Requests pro Domain (Rate-Limit)
 - nutzt dieselbe BeautifulSoup-Text-Extraktion wie web_scraper.py
 - gibt pro URL strukturierte Event-Einträge zurück, die main.py in den
   Run-JSON einbetten kann

Zusätzlich bietet es eine kleine Helfer-Funktion `brand_slug()`, mit der die
Schreibweise des Brand-Namens normalisiert wird.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from analyzer import scraping_api

# Reuse des User-Agents, damit wir gegenüber der Seite konsistent auftreten
USER_AGENT = "geo-visibility-tool/1.0 (+https://github.com/phoeser/geo-visibility-tool)"

# Pro Domain min. Delay zwischen zwei Requests (Sekunden)
DOMAIN_MIN_DELAY = 1.5

# Max. Text pro Seite, die wir speichern
MAX_TEXT_BYTES = 400_000

# Max. added/removed Lines im Event
MAX_DIFF_LINES = 120

# Max. Chars der Diff-Snippets für den Classifier
MAX_CLASSIFIER_SNIPPET = 6000

# Schwelle fuer "echte" Aenderungen:
# Wenn Textaehnlichkeit >= NOISE_SIMILARITY (97%) UND Diff kleiner als NOISE_MAX_LINES
# Zeilen betraegt, behandeln wir das als dynamisches Rauschen (rotierende Teaser,
# Testimonials etc.) und erzeugen KEIN change-Event.
NOISE_SIMILARITY = 0.97
NOISE_MAX_LINES = 10

# Loeschungs-Erkennung: max. Anzahl verwaister Seiten (frueher getrackt, aber
# nicht mehr in Sitemap/tracked_urls), die pro Lauf erneut geprueft werden.
# Aelteste last_seen zuerst; nach einem removed-Event wird nicht mehr angefragt.
#
# 20.07.2026 von 400 auf 120 gesenkt — mit Begruendung aus einem echten Fehllauf:
# Lauf #165 lief in den 5-Stunden-Timeout. Ursachen in dieser Reihenfolge:
#   1. Markenerweiterung 7 -> 25: der Seiten-Crawl waechst auf 5.793 URLs ueber
#      24 Marken (vorher rund 3.000).
#   2. Perplexity lief erstmals mit Guthaben wirklich durch -> LLM-Phase 3h40
#      statt bisher gut 1,5 h.
#   3. Diese Orphan-Pruefung legte 400 weitere Abrufe obendrauf — und zwar die
#      teuersten: verwaiste Seiten sind haeufig tot und laufen in den Timeout.
# Punkt 3 war der kleinste Beitrag, aber der einzige, der ohne Informationsverlust
# schrumpfbar ist. Loeschungen sind ein langsames Phaenomen; 120 Seiten pro Lauf
# arbeiten den Rueckstand in wenigen Tagen ab, weil erfolgreich abgerufene Seiten
# ein frisches last_seen bekommen und ans Ende der Warteschlange rotieren.
MAX_ORPHAN_RECHECKS = 120

# Kuerzerer Timeout fuer Orphan-Abrufe. 20.07.2026 Review-Fix: Die Konstante war
# TOTER CODE — track_page ruft _fetch(url) ohne Timeout-Argument, also mit dem
# Default von 30 s, und collect_orphan_pages reicht die Orphan-Eigenschaft gar nicht
# weiter. Die Haelfte der Laufzeit-Massnahme aus Lauf #165 war damit unwirksam,
# waehrend die Doku sie als umgesetzt fuehrte. Jetzt tatsaechlich verdrahtet:
# track_all markiert Orphans, track_page nutzt den kuerzeren Timeout.
ORPHAN_FETCH_TIMEOUT = 12


# ---------------------------------------------------------------------------
# Crawl-Fix (26.07.2026): Sperrliste dauerhaft blockierter URLs
# ---------------------------------------------------------------------------
# URLs, die auch nach dem FlareSolverr-Fallback blockiert bleiben (403/429/5xx
# oder Timeout), werden mit Zeitstempel vermerkt und BLOCK_COOLDOWN_DAYS lang gar
# nicht mehr angefragt. Einzige Massnahme, die den Aufwand dauerhaft SENKT statt
# ihn nur zu deckeln: ab dem zweiten Lauf kosten dieselben toten URLs 0 s.
# Persistenz in data/blocked_urls.json (relativ zu pages_base.parent), wird vom
# Workflow mitcommittet. 404/410 sind bewusst NICHT dabei - die laufen weiter in
# die Loeschungs-Erkennung (removed-Event).
BLOCK_LIST_FILE = Path("data/blocked_urls.json")
BLOCK_COOLDOWN_DAYS = 7
BLOCK_COOLDOWN_SECONDS = BLOCK_COOLDOWN_DAYS * 24 * 3600
_BLOCK_STATUSES = {0, 401, 402, 403, 407, 429, 502, 503, 504}

# 05.08.2026 — WACHSENDER Cooldown statt fixer 7 Tage.
# Messung am Lauf 2026-08-05T00-14-31Z: 696 der 6.115 Abrufe waren gesperrt
# (kosten 0 s), 97 liefen wirklich in den Fehler. Jeder solche Fehlversuch kostet
# bis zu 30 s (requests-Timeout) + 35 s (FlareSolverr-Fallback) = 65 s Thread-Zeit.
# Bei fixem 7-Tage-Cooldown laufen die 816 Listeneintraege in Wochenfrist alle
# einmal wieder auf — rund 116 Wiederholungen pro Nacht, davon scheitert die grosse
# Mehrheit erneut: 116 x 65 s / 10 Worker = rund 12 min je Lauf, jede Nacht.
# Mit Verdopplung je erneutem Fehlschlag (7 -> 14 -> 28 Tage, Deckel 28) sinkt die
# Wiederholrate dauerhaft auf ein Viertel. KEIN Informationsverlust: jede URL wird
# weiterhin regelmaessig nachgeprueft, nur seltener, und ein einziger Erfolg setzt
# den Zaehler sofort zurueck (unmark_url_blocked im Erfolgspfad von track_page).
BLOCK_COOLDOWN_MAX_DAYS = 28
BLOCK_COOLDOWN_MAX_SECONDS = BLOCK_COOLDOWN_MAX_DAYS * 24 * 3600
# Eintraege werden laenger aufbewahrt als der Cooldown, sonst geht der Fehlzaehler
# beim Ablauf verloren und die Eskalation begaenne jedes Mal wieder bei 7 Tagen.
BLOCK_RETENTION_SECONDS = 120 * 24 * 3600

_block_lock = threading.Lock()
# url -> {"ts": float, "fails": int}
_block_list: Dict[str, Dict] = {}


# ---------------------------------------------------------------------------
# Laufzeit-Messung (05.08.2026)
# ---------------------------------------------------------------------------
# Zaehlt Thread-Sekunden je Teilschritt der Seiten-Phase, damit die naechste
# Laufzeit-Analyse nicht wieder rekonstruiert werden muss. Rein additiv: die
# Werte landen in LAST_TRACK_STATS und von dort in run["timings"]["pages"].
# Ein Fehler hier darf den Lauf nie kippen -> alle Zugriffe sind exception-frei.

class _PhaseStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._d: Dict[str, float] = {}

    def reset(self) -> None:
        with self._lock:
            self._d = {}

    def add(self, key: str, value: float) -> None:
        try:
            with self._lock:
                self._d[key] = self._d.get(key, 0.0) + float(value)
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> Dict[str, float]:
        try:
            with self._lock:
                return {k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in self._d.items()}
        except Exception:  # noqa: BLE001
            return {}


_stats = _PhaseStats()
LAST_TRACK_STATS: Dict[str, float] = {}


def _cooldown_for(fails: int) -> float:
    """7 Tage beim ersten Fehlschlag, danach Verdopplung bis BLOCK_COOLDOWN_MAX."""
    try:
        n = max(1, int(fails))
    except (TypeError, ValueError):
        n = 1
    return min(BLOCK_COOLDOWN_SECONDS * (2 ** (n - 1)), BLOCK_COOLDOWN_MAX_SECONDS)


def load_block_list(path: Path = BLOCK_LIST_FILE) -> None:
    """Sperrliste aus JSON laden. Akzeptiert das alte Format (url -> ts) und das
    neue (url -> {"ts": ..., "fails": ...}). Fehlt/kaputt -> leer."""
    global _block_list
    loaded: Dict[str, Dict] = {}
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for u, val in (raw or {}).items():
                try:
                    if isinstance(val, dict):
                        loaded[u] = {"ts": float(val.get("ts") or 0.0),
                                     "fails": int(val.get("fails") or 1)}
                    else:
                        loaded[u] = {"ts": float(val), "fails": 1}
                except (TypeError, ValueError):
                    continue
    except Exception as ex:  # noqa: BLE001
        print(f"[BLOCKLIST] Laden fehlgeschlagen: {ex}")
        loaded = {}
    with _block_lock:
        _block_list = loaded
    print(f"[BLOCKLIST] {len(loaded)} URL(s) auf Sperrliste geladen.")


def save_block_list(path: Path = BLOCK_LIST_FILE) -> None:
    """Sperrliste schreiben; sehr alte Eintraege (Retention) fallen raus."""
    now = time.time()
    with _block_lock:
        keep = {u: v for u, v in _block_list.items()
                if now - float(v.get("ts") or 0.0) < BLOCK_RETENTION_SECONDS}
        _block_list.clear()
        _block_list.update(keep)
        snapshot = {u: dict(v) for u, v in keep.items()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                                   sort_keys=True), encoding="utf-8")
        active = sum(1 for v in snapshot.values()
                     if now - float(v.get("ts") or 0.0) < _cooldown_for(v.get("fails")))
        print(f"[BLOCKLIST] {len(snapshot)} URL(s) geschrieben -> {path} "
              f"({active} davon aktuell im Cooldown)")
    except Exception as ex:  # noqa: BLE001
        print(f"[BLOCKLIST] Schreiben fehlgeschlagen: {ex}")


def is_url_blocked(url: str, now: Optional[float] = None) -> bool:
    """True, wenn die URL innerhalb ihres (wachsenden) Cooldowns gesperrt ist."""
    now = time.time() if now is None else now
    with _block_lock:
        entry = _block_list.get(url)
    if not entry:
        return False
    try:
        ts = float(entry.get("ts") or 0.0)
    except (TypeError, ValueError):
        return False
    return (now - ts) < _cooldown_for(entry.get("fails"))


def mark_url_blocked(url: str, now: Optional[float] = None) -> None:
    """URL als blockiert vermerken; Fehlzaehler hoch, Cooldown startet ab jetzt."""
    now = time.time() if now is None else now
    with _block_lock:
        prev = _block_list.get(url) or {}
        try:
            fails = int(prev.get("fails") or 0)
        except (TypeError, ValueError):
            fails = 0
        _block_list[url] = {"ts": now, "fails": fails + 1}


def unmark_url_blocked(url: str) -> None:
    """Erfolgreicher Abruf -> Eintrag loeschen, Eskalation beginnt bei 0."""
    with _block_lock:
        _block_list.pop(url, None)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def brand_slug(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_") or "unknown"


def url_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    }


WS_RE = re.compile(r"\s+")


def _extract_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer"]):
        tag.decompose()
    body = soup.body or soup
    raw = body.get_text(separator="\n")
    lines = [WS_RE.sub(" ", line).strip() for line in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        text = text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
    return text


def _extract_content_dates(html: str) -> Dict[str, Optional[str]]:
    """Veroeffentlichungs-/Aenderungsdatum aus dem Roh-HTML ziehen (02.08.2026):
    schema.org JSON-LD (datePublished/dateModified), OpenGraph
    (article:published_time/modified_time), gaengige <meta>/<time>. Liefert ISO-
    Datum (YYYY-MM-DD) oder None. Zweck: 'echt neue/aktualisierte Seite' von
    'unser Crawler hat die URL erstmals gesehen' trennen — Grundlage der
    retrospektiven Wirkungs-Auswertung neuer Seiten."""
    if not html:
        return {"published": None, "modified": None}
    pub = mod = None

    def _norm(s):
        if not s:
            return None
        m = re.search(r"(\d{4}-\d{2}-\d{2})", str(s))
        return m.group(1) if m else None

    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        stack = [data]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if not pub:
                    pub = _norm(o.get("datePublished") or o.get("dateCreated"))
                if not mod:
                    mod = _norm(o.get("dateModified"))
                stack.extend(v for v in o.values() if isinstance(v, (dict, list)))
            elif isinstance(o, list):
                stack.extend(o)
    if not pub:
        m = re.search(r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|datePublished|date)["\'][^>]+content=["\']([^"\']+)', html, re.I)
        pub = _norm(m.group(1)) if m else pub
    if not mod:
        m = re.search(r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:modified_time|dateModified|og:updated_time)["\'][^>]+content=["\']([^"\']+)', html, re.I)
        mod = _norm(m.group(1)) if m else mod
    if not (pub or mod):
        m = re.search(r'<time[^>]+datetime=["\']([^"\']+)', html, re.I)
        if m:
            pub = _norm(m.group(1))
    return {"published": pub, "modified": mod}


# ---------------------------------------------------------------------------
# Sitemap-<lastmod> (04.08.2026)
#
# Quelle: analyzer/sitemap_discovery.py sammelt beim ohnehin stattfindenden
# Sitemap-Abruf je URL das <lastmod> ein. main.py reicht diesen Index hier
# herein. Es entstehen dadurch KEINE zusaetzlichen HTTP-Abrufe und es aendert
# sich nichts an Auswahl, Reihenfolge oder Zahl der gecrawlten Seiten.
#
# SEMANTIK (der wichtige Teil):
#   published        = gemessen, aus dem Seiten-HTML (schema.org/OG/<time>)
#   sitemap_lastmod  = gemessen, aber ANDERE Groesse: letzte Aenderung laut
#                      Sitemap, NICHT das Veroeffentlichungsdatum.
#   published_obergrenze = geschaetzt. Nur gesetzt, wenn published fehlt. Es gilt
#                      lediglich "veroeffentlicht <= lastmod", weil eine Seite
#                      nicht nach ihrer letzten Aenderung entstanden sein kann.
# sitemap_lastmod ueberschreibt published nie und wird nie als published
# ausgegeben.
# ---------------------------------------------------------------------------

# Feldnamen in meta.json (Praefix "page_" analog zu page_published/page_modified)
META_SITEMAP_LASTMOD = "page_sitemap_lastmod"
META_SITEMAP_LASTMOD_SEEN = "page_sitemap_lastmod_gesehen_am"


def _sitemap_lastmod_for(url: str, index: Optional[Dict[str, dict]]) -> Optional[dict]:
    """Sucht den Sitemap-Eintrag zu einer getrackten URL. Sitemap- und
    Config-Schreibweise unterscheiden sich haeufig (www., Trailing-Slash,
    http/https), deshalb ueber den normalisierten Schluessel."""
    if not index or not url:
        return None
    try:
        from analyzer.sitemap_discovery import normalize_url_key
        return index.get(normalize_url_key(url))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Rate-Limiter (domain-scoped)
# ---------------------------------------------------------------------------

class DomainRateLimiter:
    def __init__(self, min_delay: float = DOMAIN_MIN_DELAY):
        self.min_delay = min_delay
        self._last: Dict[str, float] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _domain_lock(self, host: str) -> threading.Lock:
        with self._guard:
            lk = self._locks.get(host)
            if lk is None:
                lk = threading.Lock()
                self._locks[host] = lk
            return lk

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        # Pro Domain serialisiert (hoeflich), verschiedene Domains parallel.
        with self._domain_lock(host):
            now = time.time()
            last = self._last.get(host, 0.0)
            wait = max(0.0, self.min_delay - (now - last))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.time()


# ---------------------------------------------------------------------------
# robots.txt-Compliance (pro Domain gecacht)
# ---------------------------------------------------------------------------

class RobotsCache:
    """
    Holt robots.txt mit unserem Browser-UA (statt Pythons default urllib).
    Erkennt Cloudflare-Block-Pages und wertet sie als "kein robots" -> allow.
    Globaler Override via cfg.respect_robots_txt (default True).
    """

    def __init__(self, respect: bool = True) -> None:
        self.respect = respect
        self._cache: Dict[str, Optional[RobotFileParser]] = {}
        self._lock = threading.Lock()

    def _load(self, host: str) -> Optional[RobotFileParser]:
        try:
            r = requests.get(
                f"https://{host}/robots.txt",
                headers=_headers(),
                timeout=10,
                allow_redirects=True,
            )
            # Cloudflare/AntiBot oder andere Block-Pages: kein gueltiges robots.txt
            if r.status_code != 200:
                return None
            txt = r.text or ""
            low = txt.lower()
            cf_signals = ("cf-challenge", "cf_chl_opt", "/cdn-cgi/challenge",
                          "<!doctype html", "<html")
            if any(s in low for s in cf_signals):
                # HTML statt robots.txt - Cloudflare oder JS-Challenge
                return None
            rp = RobotFileParser()
            rp.parse(txt.splitlines())
            return rp
        except Exception:
            return None

    def allowed(self, url: str) -> bool:
        if not self.respect:
            return True  # Master-Switch via config
        host = urlparse(url).netloc
        with self._lock:
            if host not in self._cache:
                self._cache[host] = self._load(host)
            rp = self._cache[host]
        if rp is None:
            # Kein lesbares robots.txt (z.B. Cloudflare-blockiert) -> erlaubt.
            return True
        return rp.can_fetch(USER_AGENT, url)


# ---------------------------------------------------------------------------
# Storage-Layer
# ---------------------------------------------------------------------------

def _page_dir(base: Path, brand: str, url: str) -> Path:
    return base / brand_slug(brand) / url_hash(url)


def _read_json(p: Path) -> Optional[dict]:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_events(pages_base: Path, brand: str, url: Optional[str] = None) -> List[dict]:
    """
    Lädt alle Events eines Brands (optional nur für eine URL).
    """
    brand_dir = pages_base / brand_slug(brand)
    if not brand_dir.exists():
        return []
    out: List[dict] = []
    if url is not None:
        files = [_page_dir(pages_base, brand, url) / "events.jsonl"]
    else:
        files = sorted(brand_dir.glob("*/events.jsonl"))
    for fp in files:
        if not fp.exists():
            continue
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _diff_lines(prev: str, curr: str) -> Tuple[List[str], List[str], float]:
    if prev == curr:
        return [], [], 1.0
    ratio = difflib.SequenceMatcher(a=prev, b=curr).ratio()
    added, removed = [], []
    for line in difflib.unified_diff(prev.splitlines(), curr.splitlines(), lineterm="", n=0):
        if line.startswith(("+++ ", "--- ", "@@")):
            continue
        if line.startswith("+"):
            added.append(line[1:].strip())
        elif line.startswith("-"):
            removed.append(line[1:].strip())
    added = [a for a in added if a]
    removed = [r for r in removed if r]
    # Verschobene Zeilen (in beiden Listen) = Umsortierung, keine echte
    # Aenderung -> aus beiden entfernen (behebt "entfernt == hinzugefuegt").
    from collections import Counter as _Counter
    _moved = _Counter(added) & _Counter(removed)
    def _strip_moved(_lines):
        _c = dict(_moved); _out = []
        for _x in _lines:
            if _c.get(_x, 0) > 0:
                _c[_x] -= 1
            else:
                _out.append(_x)
        return _out
    added = _strip_moved(added)[:MAX_DIFF_LINES]
    removed = _strip_moved(removed)[:MAX_DIFF_LINES]
    return added, removed, round(ratio, 4)


# ---------------------------------------------------------------------------
# Fetch + Track
# ---------------------------------------------------------------------------

@dataclass
class TrackResult:
    url: str
    brand: str
    product_ids: List[str] = field(default_factory=list)
    status: int = 0
    error: Optional[str] = None
    changed: bool = False
    first_seen: bool = False
    text_hash: str = ""
    prev_hash: str = ""
    similarity: float = 1.0
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)
    summary: str = ""
    classification: Optional[dict] = None


def _fetch(url: str, timeout: int = 30) -> Tuple[int, str, Optional[str]]:
    """Holt eine URL. Fallback auf ScrapingBee wenn 403/Cloudflare erkannt."""
    try:
        r = requests.get(url, headers=_headers(), timeout=timeout, allow_redirects=True)
        status = r.status_code
        text = r.text if r.ok else None

        # Cloudflare-Erkennung auch bei 200er Response (Challenge-Page)
        if text and scraping_api.looks_like_cloudflare_challenge(text):
            status = 403  # als blockiert betrachten

        if status in (0, 401, 402, 403, 407, 429, 502, 503, 504) or text is None:
            # Fallback via ScrapingBee, wenn API-Key verfuegbar
            if scraping_api.api_key_available():
                bee_status, bee_final, bee_html = scraping_api.fetch_via_api(
                    url, render_js=False, premium=True
                )
                if bee_status == 200 and bee_html and not scraping_api.looks_like_cloudflare_challenge(bee_html):
                    print(f"[SCRAPINGBEE] {url}: OK via Fallback")
                    return 200, bee_final or url, bee_html
                # 26.07.2026: 2. Versuch mit render_js=True gestrichen - er verdoppelte
                # den Worst Case und half praktisch nie (das Hindernis ist die Challenge,
                # nicht das Rendering).
                print(f"[SCRAPINGBEE] {url}: Fallback fehlgeschlagen (status {bee_status})")
            else:
                print(f"[FETCH] {url}: {status} - kein ScrapingBee-Key, ueberspringe")
        return status, r.url, text
    except Exception as e:  # noqa: BLE001
        # Bei Connection-Exception/Timeout (häufig bei Cloudflare) trotzdem
        # FlareSolverr-Fallback probieren - da ist FlareSolverr genau für gemacht.
        if scraping_api.api_key_available():
            print(f"[FETCH] {url}: Exception '{type(e).__name__}: {str(e)[:80]}' - probiere FlareSolverr")
            try:
                bee_status, bee_final, bee_html = scraping_api.fetch_via_api(
                    url, render_js=False, premium=True
                )
                if bee_status == 200 and bee_html and not scraping_api.looks_like_cloudflare_challenge(bee_html):
                    print(f"[FLARESOLVERR] {url}: OK trotz Connection-Exception")
                    return 200, bee_final or url, bee_html
                # 26.07.2026: 2. render_js-Versuch gestrichen (siehe _fetch success-Pfad).
            except Exception as e2:
                print(f"[FLARESOLVERR] {url}: Fallback-Exception: {e2}")
        return 0, url, None

def track_page(
    pages_base: Path,
    brand: str,
    product_ids: List[str],
    url: str,
    *,
    timestamp: str,
    run_id: str,
    rate_limiter: DomainRateLimiter,
    robots: RobotsCache,
    classifier=None,
    is_orphan: bool = False,
    sitemap_lastmod: Optional[dict] = None,
) -> TrackResult:
    """
    Holt eine einzelne URL, vergleicht mit dem letzten Stand, schreibt
    current.json + events.jsonl, ruft optional den Classifier auf.

    `classifier` ist ein Callable(url, added_lines, removed_lines, summary) -> dict | None.
    Wenn None, wird keine Klassifikation angehängt.
    """
    result = TrackResult(url=url, brand=brand, product_ids=list(product_ids))

    if not robots.allowed(url):
        result.error = "robots.txt disallow"
        return result

    if is_url_blocked(url):
        result.error = "blocked (Sperrliste, Cooldown aktiv)"
        _stats.add("blocked_skipped", 1)
        return result

    _t_wait = time.perf_counter()
    rate_limiter.wait(url)
    _t_fetch = time.perf_counter()
    _stats.add("ratelimit_seconds", _t_fetch - _t_wait)
    try:
        status, final_url, html = _fetch(url, timeout=(ORPHAN_FETCH_TIMEOUT if is_orphan else 30))
    finally:
        _stats.add("fetch_seconds", time.perf_counter() - _t_fetch)
        _stats.add("fetches", 1)
    result.status = status

    # Crawl-Fix 26.07.2026: bleibt die URL nach dem Fallback blockiert (403/429/5xx
    # oder Timeout=0), auf die Sperrliste - 404/410 nicht, die gehen in die
    # Loeschungs-Erkennung.
    if status in _BLOCK_STATUSES:
        mark_url_blocked(url)
        _stats.add("blocked_marked", 1)
    elif status == 200:
        # Seite lebt wieder -> Eskalationszaehler zuruecksetzen.
        unmark_url_blocked(url)

    # 404 / 410 / Server-Errors explizit behandeln
    if status in (0, 404, 410):
        result.error = f"HTTP {status}" if status else "fetch failed (timeout/exception)"
        # 2026-06-05: Seitenloeschung als Event erfassen (einmalig) — nur wenn
        # die Seite frueher erfolgreich erfasst wurde (current.json existiert)
        if status in (404, 410):
            try:
                _pd = _page_dir(pages_base, brand, url)
                _cur = _pd / "current.json"
                _ev = _pd / "events.jsonl"
                if _cur.exists():
                    _already = False
                    if _ev.exists():
                        _lines = _ev.read_text(encoding="utf-8").strip().splitlines()
                        if _lines:
                            _already = json.loads(_lines[-1]).get("event_type") == "removed"
                    if not _already:
                        _prev = _read_json(_cur) or {}
                        _append_jsonl(_ev, {
                            "timestamp": timestamp, "run_id": run_id, "brand": brand,
                            "product_ids": list(product_ids), "url": url,
                            "event_type": "removed",
                            "hash_before": _prev.get("text_hash", ""), "hash_after": "",
                            "similarity": 0.0, "added_lines_count": 0,
                            "removed_lines_count": 0, "added_lines": [], "removed_lines": [],
                            "summary": f"Seite nicht mehr erreichbar (HTTP {status}).",
                            "classification": None,
                        })
            except Exception:
                pass
        return result
    if status >= 400:
        result.error = f"HTTP {status}"
        return result
    if not html:
        result.error = f"empty body (status {status})"
        return result

    text = _extract_text(html)
    if not text:
        result.error = "empty text after extract"
        return result

    _dates = _extract_content_dates(html)

    page_dir = _page_dir(pages_base, brand, url)
    meta_path = page_dir / "meta.json"
    current_path = page_dir / "current.json"
    events_path = page_dir / "events.jsonl"

    prev = _read_json(current_path) or {}
    prev_text = prev.get("text", "")
    prev_hash = prev.get("text_hash", "")
    new_hash = _sha16(text)
    result.text_hash = new_hash
    result.prev_hash = prev_hash

    first_seen = not current_path.exists()
    result.first_seen = first_seen
    changed = first_seen or (new_hash != prev_hash)
    result.changed = changed

    # Meta (erzeugen/aktualisieren)
    meta = _read_json(meta_path) or {
        "url": url,
        "brand": brand,
        "product_ids": list(product_ids),
        "first_seen": timestamp,
    }
    # Produkt-Zuordnung zusammenführen (URL kann zu mehreren Produkten gehören)
    pids = set(meta.get("product_ids", []))
    pids.update(product_ids)
    meta["product_ids"] = sorted(pids)
    meta["last_seen"] = timestamp
    if _dates.get("published"):
        meta["page_published"] = meta.get("page_published") or _dates["published"]
    if _dates.get("modified"):
        meta["page_modified"] = _dates["modified"]
    # Sitemap-<lastmod>: eigenes Feld, ueberschreibt page_published NICHT.
    # Wie page_modified wird der jeweils aktuellste Wert gehalten.
    if sitemap_lastmod and sitemap_lastmod.get("sitemap_lastmod"):
        meta[META_SITEMAP_LASTMOD] = sitemap_lastmod["sitemap_lastmod"]
        meta[META_SITEMAP_LASTMOD_SEEN] = sitemap_lastmod.get("sitemap_lastmod_gesehen_am")
    _write_json(meta_path, meta)

    # current.json immer aktualisieren (überschreibt)
    _write_json(current_path, {
        "url": url,
        "brand": brand,
        "product_ids": list(product_ids),
        "text": text,
        "text_hash": new_hash,
        "status": status,
        "timestamp": timestamp,
    })

    if first_seen:
        result.summary = "Seite erstmalig erfasst."
        event = {
            "timestamp": timestamp,
            "run_id": run_id,
            "brand": brand,
            "product_ids": list(product_ids),
            "url": url,
            "event_type": "first_seen",
            "hash_before": "",
            "hash_after": new_hash,
            "similarity": 0.0,
            "added_lines_count": 0,
            "removed_lines_count": 0,
            "added_lines": [],
            "removed_lines": [],
            "summary": result.summary,
            "classification": None,
            "page_published": _dates.get("published"),
            "page_modified": _dates.get("modified"),
        }
        _append_jsonl(events_path, event)
        return result

    if not changed:
        result.summary = "Keine Veränderung."
        return result

    added, removed, similarity = _diff_lines(prev_text, text)
    result.added_lines = added
    result.removed_lines = removed
    result.similarity = similarity
    result.summary = (
        f"{len(added)} neue Zeilen, {len(removed)} entfernte Zeilen "
        f"(Ähnlichkeit {similarity:.1%})."
    )

    # Rauschfilter: sehr aehnliche Seiten mit winzigen Diffs = dynamische Teaser
    if (len(added) + len(removed)) == 0 or (similarity >= NOISE_SIMILARITY and (len(added) + len(removed)) <= NOISE_MAX_LINES):
        result.changed = False
        result.summary = (
            f"Keine substantielle Aenderung (Ähnlichkeit {similarity:.1%}, "
            f"nur {len(added)+len(removed)} Zeilen Diff - Rauschen)."
        )
        return result

    classification = None
    if classifier is not None:
        _t_cls = time.perf_counter()
        try:
            classification = classifier(url, added, removed, result.summary)
        except Exception as e:  # noqa: BLE001
            classification = {"error": str(e)[:200]}
        finally:
            _stats.add("classify_seconds", time.perf_counter() - _t_cls)
            _stats.add("classify_calls", 1)
    result.classification = classification

    event = {
        "timestamp": timestamp,
        "run_id": run_id,
        "brand": brand,
        "product_ids": list(product_ids),
        "url": url,
        "event_type": "change",
        "hash_before": prev_hash,
        "hash_after": new_hash,
        "similarity": similarity,
        "added_lines_count": len(added),
        "removed_lines_count": len(removed),
        "added_lines": added,
        "removed_lines": removed,
        "summary": result.summary,
        "classification": classification,
        "page_published": _dates.get("published"),
        "page_modified": _dates.get("modified") or _dates.get("published"),
    }
    _append_jsonl(events_path, event)
    return result


# ---------------------------------------------------------------------------
# Convenience: run over a full URL-Matrix
# ---------------------------------------------------------------------------

def _last_event_type(events_path: Path) -> str:
    """Liefert den event_type des letzten Events in events.jsonl (oder '')."""
    try:
        if not events_path.exists():
            return ""
        lines = events_path.read_text(encoding="utf-8").strip().splitlines()
        if not lines:
            return ""
        return (json.loads(lines[-1]) or {}).get("event_type", "") or ""
    except Exception:
        return ""


def collect_orphan_pages(pages_base: Path, active_urls: set) -> List[Tuple[str, List[str], str]]:
    """
    Findet frueher getrackte Seiten, die in der aktuellen URL-Liste nicht mehr
    auftauchen (z.B. aus der Sitemap verschwunden).

    Hintergrund: Ohne erneuten Abruf wuerde eine Loeschung NIE bemerkt — die
    Seite faellt aus der Sitemap, wird nie wieder angefragt, der 404 wird nie
    gesehen, "Geloeschte Seiten" bleibt fuer immer leer. Darum werden solche
    Seiten weiter mitgeprueft, bis sie ein removed-Event haben (danach nicht
    mehr, damit tote URLs nicht ewig angefragt werden).
    """
    if not pages_base.exists():
        return []
    orphans: List[Tuple[str, List[str], str, str]] = []
    seen: set = set()
    for meta_path in sorted(pages_base.glob("*/*/meta.json")):
        page_dir = meta_path.parent
        if not (page_dir / "current.json").exists():
            continue
        meta = _read_json(meta_path) or {}
        url = (meta.get("url") or "").strip()
        if not url or url in active_urls or url in seen:
            continue
        seen.add(url)
        if _last_event_type(page_dir / "events.jsonl") == "removed":
            continue
        brand = meta.get("brand") or page_dir.parent.name
        pids = list(meta.get("product_ids") or [])
        orphans.append((brand, pids, url, meta.get("last_seen") or ""))
    # Aelteste zuerst: laengst nicht mehr gesehene Seiten haben Prioritaet;
    # noch erreichbare Orphans bekommen ein frisches last_seen und rotieren
    # dadurch ans Ende.
    orphans.sort(key=lambda t: t[3])
    if len(orphans) > MAX_ORPHAN_RECHECKS:
        print(f"[page_tracker] {len(orphans)} verwaiste Seiten — pruefe nur die aeltesten {MAX_ORPHAN_RECHECKS}")
        orphans = orphans[:MAX_ORPHAN_RECHECKS]
    return [(b, p, u) for (b, p, u, _ls) in orphans]


def _annotate_sitemap_lastmod(records: Dict[str, dict]) -> None:
    """Wertet die eingesammelten sitemap_lastmod-Werte je MARKE aus (in-place):

    1. Massenstempel-Test: traegt eine Marke fuer >80 % ihrer datierten URLs
       dasselbe <lastmod>, ist das der Zeitpunkt des letzten CMS-Deployments und
       kein Inhaltsdatum. Diese Seiten bekommen sitemap_lastmod_unbrauchbar=true
       plus Grund und werden NICHT zu einer Obergrenze verrechnet.
    2. Sonst: fehlt `published`, wird `published_obergrenze` gesetzt — mit dem
       expliziten Hinweis, dass es sich um eine Schranke handelt (veroeffentlicht
       <= lastmod) und nicht um ein gemessenes Datum.
    Bereits vorhandene Felder (published/modified) werden nie angefasst.
    """
    try:
        from analyzer.sitemap_discovery import mass_stamp_verdict
    except Exception:  # noqa: BLE001
        return
    by_brand: Dict[str, List[str]] = {}
    for rec in records.values():
        lm = rec.get("sitemap_lastmod")
        if lm:
            by_brand.setdefault(rec.get("brand") or "", []).append(lm)
    verdicts = {b: mass_stamp_verdict(vals) for b, vals in by_brand.items()}
    for rec in records.values():
        lm = rec.get("sitemap_lastmod")
        if not lm:
            continue
        v = verdicts.get(rec.get("brand") or "")
        if v:
            rec["sitemap_lastmod_unbrauchbar"] = True
            rec["sitemap_lastmod_unbrauchbar_grund"] = v["grund"]
            continue
        if rec.get("published"):
            continue  # gemessenes Datum vorhanden — lastmod aendert daran nichts
        rec["published_obergrenze"] = lm
        rec["published_obergrenze_quelle"] = "sitemap_lastmod"
        rec["published_obergrenze_hinweis"] = (
            "GESCHAETZT, kein gemessenes Datum: die Seite liefert kein "
            "Veroeffentlichungsdatum aus. Aus <lastmod> folgt nur "
            "'veroeffentlicht <= " + lm + "'; tatsaechlich kann sie beliebig "
            "aelter sein. Nicht als published verwenden."
        )


def write_page_dates(pages_base: Path,
                     sitemap_lastmods: Optional[Dict[str, dict]] = None) -> None:
    """Konsolidiert Publikations-/Aenderungsdaten aller getrackten Seiten in EINE
    Datei data/page_dates.json. Zweck: das Cockpit joint die echten Veroeffentlichungs-
    daten gegen page_new/change-Events, ohne tausende meta.json zu lesen —
    Grundlage der retrospektiven Neue-Seiten-Auswertung (02.08.2026).

    DATENMODELL je URL — gemessen vs. geschaetzt sauber getrennt:
      published        GEMESSEN. Veroeffentlichungsdatum aus dem Seiten-HTML
                       (schema.org datePublished / OG article:published_time /
                       <time>). null, wenn die Seite keines ausliefert.
      modified         GEMESSEN. Aenderungsdatum aus demselben HTML.
      first_seen       GEMESSEN, aber nur unsere Crawler-Sicht: wann WIR die URL
                       zum ersten Mal gesehen haben.
      last_seen        GEMESSEN, letzter erfolgreicher Abruf.
      sitemap_lastmod  GEMESSEN, aber eine ANDERE Groesse: <lastmod> aus der
                       sitemap.xml = letzte AENDERUNG. Das ist NICHT das
                       Veroeffentlichungsdatum und wird nie als solches gefuehrt.
      sitemap_lastmod_gesehen_am  Wann wir dieses <lastmod> gelesen haben.
      sitemap_lastmod_unbrauchbar / ..._grund
                       true, wenn die Marke fuer >80 % ihrer datierten URLs
                       dasselbe <lastmod> traegt. Das ist ein CMS-/Deploy-
                       Zeitstempel und kein Inhaltsdatum — dann NICHT auswerten.
      published_obergrenze  GESCHAETZT. Nur gesetzt, wenn published fehlt und ein
                       brauchbares sitemap_lastmod existiert. Begruendung: eine
                       Seite kann nicht nach ihrer letzten Aenderung entstanden
                       sein, also gilt veroeffentlicht <= lastmod. Der wahre
                       Wert kann beliebig viel aelter sein — die Obergrenze ist
                       eine Schranke, kein Datum. Nie mit published mischen.
      published_obergrenze_quelle / ..._hinweis  Herkunft + Klartext-Warnung.

    `sitemap_lastmods` ist der Index aus analyzer.sitemap_discovery.lastmod_index()
    (siehe dort). Fehlt er, bleiben die Sitemap-Felder einfach leer.
    """
    out: Dict[str, dict] = {}
    try:
        for meta_path in sorted(pages_base.glob("*/*/meta.json")):
            meta = _read_json(meta_path) or {}
            url = (meta.get("url") or "").strip()
            if not url:
                continue
            rec = {
                "published": meta.get("page_published"),
                "modified": meta.get("page_modified"),
                "first_seen": meta.get("first_seen"),
                "last_seen": meta.get("last_seen"),
                "brand": meta.get("brand"),
                "product_ids": meta.get("product_ids") or [],
            }
            lm = meta.get(META_SITEMAP_LASTMOD)
            lm_seen = meta.get(META_SITEMAP_LASTMOD_SEEN)
            # Frischer Sitemap-Wert schlaegt den in meta.json gespeicherten.
            # Deckt auch Seiten ab, die in diesem Lauf gar nicht abgerufen
            # wurden (robots-Sperre, HTTP-Fehler) — deren meta.json bleibt
            # dann veraltet, page_dates.json waere sonst luecklig.
            entry = _sitemap_lastmod_for(url, sitemap_lastmods)
            if entry and entry.get("sitemap_lastmod"):
                lm = entry["sitemap_lastmod"]
                lm_seen = entry.get("sitemap_lastmod_gesehen_am") or lm_seen
                if meta.get(META_SITEMAP_LASTMOD) != lm:
                    meta[META_SITEMAP_LASTMOD] = lm
                    meta[META_SITEMAP_LASTMOD_SEEN] = lm_seen
                    try:
                        _write_json(meta_path, meta)
                    except Exception:  # noqa: BLE001
                        pass
            if lm:
                rec["sitemap_lastmod"] = lm
                rec["sitemap_lastmod_gesehen_am"] = lm_seen
            out[url] = rec
    except Exception as ex:  # noqa: BLE001
        print(f"[page_dates] Scan fehlgeschlagen: {ex}")
        return

    try:
        _annotate_sitemap_lastmod(out)
    except Exception as ex:  # noqa: BLE001
        print(f"[page_dates] lastmod-Auswertung fehlgeschlagen: {ex}")

    try:
        dst = pages_base.parent / "page_dates.json"
        dst.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        _npub = sum(1 for v in out.values() if v.get("published") or v.get("modified"))
        _nlm = sum(1 for v in out.values() if v.get("sitemap_lastmod"))
        _nob = sum(1 for v in out.values() if v.get("published_obergrenze"))
        _nun = sum(1 for v in out.values() if v.get("sitemap_lastmod_unbrauchbar"))
        print(f"[page_dates] {len(out)} Seiten geschrieben ({_npub} mit Datum) -> {dst}")
        print(f"[page_dates] sitemap_lastmod: {_nlm} Seiten, davon {_nun} unbrauchbar "
              f"(Massenstempel); {_nob} zusaetzliche published_obergrenze (geschaetzt)")
    except Exception as ex:  # noqa: BLE001
        print(f"[page_dates] Schreiben fehlgeschlagen: {ex}")


def _interleave_by_domain(tasks: List[Tuple]) -> List[Tuple]:
    """
    Sortiert die Abruf-Warteschlange so um, dass aufeinanderfolgende Tasks zu
    VERSCHIEDENEN Domains gehoeren (Round-Robin ueber die Domain-Buckets).

    Warum (Messung am Lauf 2026-08-05T00-14-31Z, 233 min Gesamtlaufzeit):
    Die Tasks entstanden bisher in Marken-Reihenfolge, also alle 801 Allianz-URLs
    am Stueck, dann 559 ADAC-URLs usw. Alle 10 Worker arbeiteten damit zur selben
    Zeit auf DERSELBEN Domain — und dort serialisiert der DomainRateLimiter sie auf
    einen Request je DOMAIN_MIN_DELAY (1,5 s). Effektive Nebenlaeufigkeit: 1 statt 10.
    Die Wanduhr war deshalb rund Summe(5.419 aktive URLs x (1,5 s + Abrufzeit)),
    also ~2,5 h Seiten-Phase.

    Nach dem Interleaving liegen zu jedem Zeitpunkt 10 verschiedene Domains in
    Arbeit (es gibt 50). Die Untergrenze ist damit die groesste Domain:
    801 x (1,5 s + Abruf) statt Summe ueber alle Domains.

    Wichtig: Die Hoeflichkeit je Domain bleibt UNveraendert — DOMAIN_MIN_DELAY
    gilt weiterhin, pro Domain wird der Abstand zwischen zwei Requests nicht
    verkleinert. Es aendert sich nur, welche Domains gleichzeitig drankommen.
    Es werden weder URLs weggelassen noch Reihenfolge-Semantik gebraucht: das
    Ergebnis ist eine Menge von Events, die Reihenfolge ist irrelevant.
    """
    try:
        buckets: Dict[str, List[Tuple]] = {}
        for t in tasks:
            host = ""
            try:
                host = urlparse(t[2]).netloc.lower()
            except Exception:  # noqa: BLE001
                host = ""
            buckets.setdefault(host, []).append(t)
        if len(buckets) <= 1:
            return list(tasks)
        # Groesste Domain zuerst -> ihre Eintraege werden am gleichmaessigsten verteilt
        ordered = sorted(buckets.values(), key=len, reverse=True)
        out: List[Tuple] = []
        i = 0
        while len(out) < len(tasks):
            progressed = False
            for b in ordered:
                if i < len(b):
                    out.append(b[i])
                    progressed = True
            if not progressed:
                break
            i += 1
        return out if len(out) == len(tasks) else list(tasks)
    except Exception as ex:  # noqa: BLE001
        print(f"[page_tracker] Interleaving fehlgeschlagen, nutze Originalreihenfolge: {ex}")
        return list(tasks)


def track_all(
    pages_base: Path,
    *,
    timestamp: str,
    run_id: str,
    brand_urls: Dict[str, List[Dict]],
    classifier=None,
    respect_robots_txt: bool = True,
    max_workers: int = 10,
    sitemap_lastmods: Optional[Dict[str, dict]] = None,
) -> List[Dict]:
    """
    brand_urls: {
        "ERGO": [{"url": "...", "product_ids": ["zahnzusatz"]}, ...],
        "Allianz": [...],
        ...
    }

    sitemap_lastmods: optionaler Index aus analyzer.sitemap_discovery
    (lastmod_index()), gefuellt beim ohnehin stattfindenden Sitemap-Abruf der
    Discovery. Rein additiv: er landet in meta.json / page_dates.json und
    beeinflusst weder Auswahl noch Reihenfolge noch Zahl der Abrufe.

    Gibt eine Liste von Tracker-Results zurück (als Dicts), damit main.py die
    als Run-JSON-Fragment speichern kann (z.B. für den Impact-Tab).
    """
    _t_start = time.perf_counter()
    _stats.reset()
    rate = DomainRateLimiter()
    robots = RobotsCache(respect=respect_robots_txt)
    block_path = pages_base.parent / BLOCK_LIST_FILE.name
    load_block_list(block_path)
    tasks: List[Tuple[str, List[str], str]] = []
    for brand, entries in brand_urls.items():
        for e in entries:
            url = e.get("url") or ""
            pids = e.get("product_ids") or []
            if url:
                tasks.append((brand, pids, url, False))

    # Loeschungs-Erkennung: frueher getrackte Seiten, die nicht mehr in der
    # aktuellen URL-Liste stehen, weiter abrufen — liefert eine solche Seite
    # HTTP 404/410, erzeugt track_page das "removed"-Event.
    try:
        orphans = collect_orphan_pages(pages_base, {t[2] for t in tasks})
        if orphans:
            print(f"[page_tracker] {len(orphans)} verwaiste Seiten (nicht mehr in Sitemap/URL-Liste) werden auf Loeschung geprueft")
            tasks.extend([(b, p, u, True) for (b, p, u) in orphans])
    except Exception as ex:  # noqa: BLE001
        print(f"[page_tracker] Orphan-Scan fehlgeschlagen: {ex}")

    def _one(brand: str, pids: List[str], url: str, is_orphan: bool = False) -> Dict:
        return asdict(track_page(
            pages_base, brand, pids, url,
            timestamp=timestamp, run_id=run_id,
            rate_limiter=rate, robots=robots,
            classifier=classifier, is_orphan=is_orphan,
            sitemap_lastmod=_sitemap_lastmod_for(url, sitemap_lastmods),
        ))

    out: List[Dict] = []
    # Seiten PARALLEL abrufen (I/O-gebunden; FlareSolverr/Cloudflare dominieren
    # die Laufzeit). Pro Domain bleibt der Abruf via Rate-Limiter serialisiert,
    # verschiedene Domains laufen gleichzeitig -> statt Stunden nur Minuten.
    # 05.08.2026: Damit das auch praktisch eintritt, wird die Warteschlange ueber
    # die Domains verschraenkt (siehe _interleave_by_domain) — vorher lagen alle
    # URLs einer Marke am Stueck und die Worker blockierten sich gegenseitig am
    # Domain-Lock derselben Domain.
    n_tasks = len(tasks)
    tasks = _interleave_by_domain(tasks)
    workers = max(1, int(max_workers))
    _finished = [False]

    def _finish():
        try:
            save_block_list(block_path)
        except Exception as ex:  # noqa: BLE001
            print(f"[page_tracker] save_block_list fehlgeschlagen: {ex}")
        try:
            write_page_dates(pages_base, sitemap_lastmods)
        except Exception as ex:  # noqa: BLE001
            print(f"[page_tracker] write_page_dates fehlgeschlagen: {ex}")
        try:
            global LAST_TRACK_STATS
            st = _stats.snapshot()
            st["tasks"] = n_tasks
            st["workers"] = workers
            st["domains"] = len({urlparse(t[2]).netloc.lower() for t in tasks})
            st["wall_seconds"] = round(time.perf_counter() - _t_start, 1)
            LAST_TRACK_STATS = st
            print("[TIMING] Seiten-Phase: "
                  f"{st.get('wall_seconds')} s Wanduhr, "
                  f"{st.get('fetches', 0)} Abrufe / {st.get('fetch_seconds', 0)} s, "
                  f"Ratelimit {st.get('ratelimit_seconds', 0)} s, "
                  f"Klassifikation {st.get('classify_calls', 0)} x "
                  f"{st.get('classify_seconds', 0)} s, "
                  f"gesperrt uebersprungen {st.get('blocked_skipped', 0)}")
        except Exception as ex:  # noqa: BLE001
            print(f"[page_tracker] Timing-Statistik fehlgeschlagen: {ex}")
        _finished[0] = True

    try:
        if workers == 1 or n_tasks <= 1:
            for (brand, pids, url, orph) in tasks:
                out.append(_one(brand, pids, url, orph))
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(_one, b, p, u, o) for (b, p, u, o) in tasks]
                for f in as_completed(futs):
                    try:
                        out.append(f.result())
                    except Exception as ex:  # noqa: BLE001
                        out.append({"error": str(ex)[:200]})
    finally:
        if not _finished[0]:
            _finish()
    return out
