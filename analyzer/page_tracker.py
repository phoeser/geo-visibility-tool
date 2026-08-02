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

_block_lock = threading.Lock()
_block_list: Dict[str, float] = {}


def load_block_list(path: Path = BLOCK_LIST_FILE) -> None:
    """Sperrliste aus JSON in den Modul-Speicher laden. Fehlt/kaputt -> leer."""
    global _block_list
    loaded: Dict[str, float] = {}
    try:
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            for u, ts in (raw or {}).items():
                try:
                    loaded[u] = float(ts)
                except (TypeError, ValueError):
                    continue
    except Exception as ex:  # noqa: BLE001
        print(f"[BLOCKLIST] Laden fehlgeschlagen: {ex}")
        loaded = {}
    with _block_lock:
        _block_list = loaded
    print(f"[BLOCKLIST] {len(loaded)} URL(s) auf Sperrliste geladen.")


def save_block_list(path: Path = BLOCK_LIST_FILE) -> None:
    """Sperrliste schreiben; Eintraege aelter als der Cooldown fallen raus."""
    now = time.time()
    with _block_lock:
        keep = {u: ts for u, ts in _block_list.items()
                if now - ts < BLOCK_COOLDOWN_SECONDS}
        _block_list.clear()
        _block_list.update(keep)
        snapshot = dict(keep)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                                   sort_keys=True), encoding="utf-8")
        print(f"[BLOCKLIST] {len(snapshot)} URL(s) geschrieben -> {path}")
    except Exception as ex:  # noqa: BLE001
        print(f"[BLOCKLIST] Schreiben fehlgeschlagen: {ex}")


def is_url_blocked(url: str, now: Optional[float] = None) -> bool:
    """True, wenn die URL innerhalb des Cooldowns gesperrt ist."""
    now = time.time() if now is None else now
    with _block_lock:
        ts = _block_list.get(url)
    return ts is not None and (now - ts) < BLOCK_COOLDOWN_SECONDS


def mark_url_blocked(url: str, now: Optional[float] = None) -> None:
    """URL als blockiert vermerken (Cooldown startet ab jetzt)."""
    now = time.time() if now is None else now
    with _block_lock:
        _block_list[url] = now


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
        return result

    rate_limiter.wait(url)
    status, final_url, html = _fetch(url, timeout=(ORPHAN_FETCH_TIMEOUT if is_orphan else 30))
    result.status = status

    # Crawl-Fix 26.07.2026: bleibt die URL nach dem Fallback blockiert (403/429/5xx
    # oder Timeout=0), auf die Sperrliste - 404/410 nicht, die gehen in die
    # Loeschungs-Erkennung.
    if status in _BLOCK_STATUSES:
        mark_url_blocked(url)

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
        try:
            classification = classifier(url, added, removed, result.summary)
        except Exception as e:  # noqa: BLE001
            classification = {"error": str(e)[:200]}
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


def write_page_dates(pages_base: Path) -> None:
    """Konsolidiert Publikations-/Aenderungsdaten aller getrackten Seiten in EINE
    Datei data/page_dates.json: {url: {published, modified, first_seen, last_seen,
    brand, product_ids}}. Zweck: das Cockpit joint die echten Veroeffentlichungs-
    daten gegen page_new/change-Events, ohne tausende meta.json zu lesen —
    Grundlage der retrospektiven Neue-Seiten-Auswertung (02.08.2026)."""
    out: Dict[str, dict] = {}
    try:
        for meta_path in sorted(pages_base.glob("*/*/meta.json")):
            meta = _read_json(meta_path) or {}
            url = (meta.get("url") or "").strip()
            if not url:
                continue
            out[url] = {
                "published": meta.get("page_published"),
                "modified": meta.get("page_modified"),
                "first_seen": meta.get("first_seen"),
                "last_seen": meta.get("last_seen"),
                "brand": meta.get("brand"),
                "product_ids": meta.get("product_ids") or [],
            }
    except Exception as ex:  # noqa: BLE001
        print(f"[page_dates] Scan fehlgeschlagen: {ex}")
        return
    try:
        dst = pages_base.parent / "page_dates.json"
        dst.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True),
                       encoding="utf-8")
        _npub = sum(1 for v in out.values() if v.get("published") or v.get("modified"))
        print(f"[page_dates] {len(out)} Seiten geschrieben ({_npub} mit Datum) -> {dst}")
    except Exception as ex:  # noqa: BLE001
        print(f"[page_dates] Schreiben fehlgeschlagen: {ex}")


def track_all(
    pages_base: Path,
    *,
    timestamp: str,
    run_id: str,
    brand_urls: Dict[str, List[Dict]],
    classifier=None,
    respect_robots_txt: bool = True,
    max_workers: int = 10,
) -> List[Dict]:
    """
    brand_urls: {
        "ERGO": [{"url": "...", "product_ids": ["zahnzusatz"]}, ...],
        "Allianz": [...],
        ...
    }

    Gibt eine Liste von Tracker-Results zurück (als Dicts), damit main.py die
    als Run-JSON-Fragment speichern kann (z.B. für den Impact-Tab).
    """
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
        ))

    out: List[Dict] = []
    # Seiten PARALLEL abrufen (I/O-gebunden; FlareSolverr/Cloudflare dominieren
    # die Laufzeit). Pro Domain bleibt der Abruf via Rate-Limiter serialisiert,
    # verschiedene Domains laufen gleichzeitig -> statt Stunden nur Minuten.
    workers = max(1, int(max_workers))
    if workers == 1 or len(tasks) <= 1:
        for (brand, pids, url, orph) in tasks:
            out.append(_one(brand, pids, url, orph))
        save_block_list(block_path)
        write_page_dates(pages_base)
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_one, b, p, u, o) for (b, p, u, o) in tasks]
        for f in as_completed(futs):
            try:
                out.append(f.result())
            except Exception as ex:  # noqa: BLE001
                out.append({"error": str(ex)[:200]})
    save_block_list(block_path)
    write_page_dates(pages_base)
    return out
