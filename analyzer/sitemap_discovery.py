"""
Sitemap-Discovery.

Für eine Domain (Marke oder Wettbewerber) findet dieses Modul alle relevanten
URLs, die zu einem Produkt passen könnten:

1. Holt `robots.txt` und extrahiert dort deklarierte Sitemaps.
2. Fällt zurück auf Standard-Pfade (`/sitemap.xml`, `/sitemap_index.xml`).
3. Folgt Sitemap-Index-Dateien bis zu den einzelnen URL-Listen.
4. Filtert URLs nach Keywords (z.B. "zahnzusatz"), optional mit URL-Path-Match.
5. Falls keine Sitemap gefunden: 1-Hop-Crawl von der Homepage aus, sammelt
   interne Links und filtert dieselben Keywords.

Das Modul ist bewusst konservativ: keine Threads, kurze Timeouts, harte
Limits auf Sitemap-Größen und Crawl-Tiefe — das Ziel ist, eine Vorschlags-
liste zu erzeugen, die der Nutzer im Config-Tab reviewed und zuschneidet.
"""

from __future__ import annotations

import gzip
import io
import re
import time
from collections import Counter
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# Browser-like UA, damit simple Bot-Filter passieren
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Begrenze, damit ein Aufruf nicht stundenlang läuft
MAX_SITEMAP_BYTES = 8 * 1024 * 1024
MAX_URLS_PER_SITEMAP = 5000
MAX_TOTAL_URLS = 20000
MAX_CRAWL_PAGES = 150  # vorher 80 - mehr Tiefe fuer neue Seiten-Erkennung
FETCH_TIMEOUT = 20


def _headers() -> Dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        "Accept": "application/xml, text/xml, text/html;q=0.9, */*;q=0.5",
    }


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def robots_txt(domain: str) -> str:
    url = f"https://{domain.rstrip('/')}/robots.txt"
    try:
        r = requests.get(url, headers=_headers(), timeout=FETCH_TIMEOUT, allow_redirects=True)
        if r.ok:
            return r.text[:200_000]
    except Exception:
        pass
    return ""


def parse_sitemaps_from_robots(robots: str) -> List[str]:
    urls: List[str] = []
    for line in robots.splitlines():
        m = re.match(r"(?i)\s*sitemap\s*:\s*(\S+)", line)
        if m:
            urls.append(m.group(1).strip())
    return urls


# ---------------------------------------------------------------------------
# sitemap.xml
# ---------------------------------------------------------------------------

def _fetch_sitemap(url: str) -> Optional[bytes]:
    try:
        r = requests.get(url, headers=_headers(), timeout=FETCH_TIMEOUT, allow_redirects=True, stream=True)
        if not r.ok:
            return None
        # Stream mit Hardlimit
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_SITEMAP_BYTES:
                break
        data = bytes(buf)
        # Gzip-Entpacken wenn URL auf .gz endet ODER Magic-Bytes erkannt werden
        try:
            if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
        except Exception:
            pass
        return data
    except Exception:
        return None


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_sitemap(xml_bytes: bytes) -> Tuple[List[str], List[str]]:
    """
    Gibt (sub_sitemaps, urls) zurück. sub_sitemaps müssen rekursiv verfolgt werden.
    """
    if not xml_bytes:
        return [], []
    # Tolerant gegen kaputtes XML: nur die Elemente rausfischen, die wir brauchen
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        # Fallback: Regex über Tag-Inhalte
        raw = xml_bytes.decode("utf-8", errors="ignore")
        sub = re.findall(r"<sitemap>\s*<loc>\s*(.*?)\s*</loc>", raw, flags=re.IGNORECASE | re.DOTALL)
        urls = re.findall(r"<url>\s*<loc>\s*(.*?)\s*</loc>", raw, flags=re.IGNORECASE | re.DOTALL)
        return sub[:MAX_URLS_PER_SITEMAP], urls[:MAX_URLS_PER_SITEMAP]

    root_tag = _strip_namespace(root.tag).lower()
    sub_sitemaps: List[str] = []
    urls: List[str] = []

    if root_tag == "sitemapindex":
        for el in root:
            if _strip_namespace(el.tag).lower() != "sitemap":
                continue
            for child in el:
                if _strip_namespace(child.tag).lower() == "loc" and child.text:
                    sub_sitemaps.append(child.text.strip())
    elif root_tag == "urlset":
        for el in root:
            if _strip_namespace(el.tag).lower() != "url":
                continue
            for child in el:
                if _strip_namespace(child.tag).lower() == "loc" and child.text:
                    urls.append(child.text.strip())

    return sub_sitemaps[:MAX_URLS_PER_SITEMAP], urls[:MAX_URLS_PER_SITEMAP]


# ---------------------------------------------------------------------------
# <lastmod> aus der Sitemap (04.08.2026)
#
# Warum: Die Discovery holt die Sitemaps ohnehin; je <url> steht dort optional
# ein <lastmod>. Bisher wurde es verworfen. Fuer Marken, deren HTML weder
# schema.org- noch OpenGraph-Datum liefert (ERGO, AXA, Signal Iduna, HDI, R+V,
# CosmosDirekt, Gothaer, Barmenia, WGV: 0 % published), ist <lastmod> die
# einzige verfuegbare Zeitinformation.
#
# WICHTIG — Semantik: <lastmod> ist das Datum der letzten AENDERUNG, nicht das
# Veroeffentlichungsdatum. Es wird deshalb NIE als `published` ausgegeben und
# ueberschreibt `published` nicht. Wo `published` fehlt, gilt lediglich
# "veroeffentlicht <= lastmod" — also eine OBERGRENZE. Diese Trennung wird bis
# in data/page_dates.json durchgehalten (Feld `published_obergrenze`).
#
# Es entstehen KEINE zusaetzlichen HTTP-Abrufe: geparst werden exakt die Bytes,
# die _fetch_sitemap fuer die URL-Liste ohnehin geholt hat.
# ---------------------------------------------------------------------------

# Ein <lastmod> unterhalb dieses Jahres ist mit hoher Wahrscheinlichkeit Muell
# (Unix-Epoche 1970, Platzhalter 1900/0001) und kein Inhaltsdatum.
LASTMOD_MIN_YEAR = 2000
# Zeitzonen-Toleranz: eine Sitemap in UTC+13 darf "morgen" schreiben, ohne dass
# wir den Wert als Zukunftsdatum verwerfen.
LASTMOD_FUTURE_TOLERANCE_DAYS = 1
# Ab so vielen datierten URLs wird der Massenstempel-Test ueberhaupt angewandt.
# Darunter ist "alle tragen dasselbe Datum" statistisch bedeutungslos.
MASS_STAMP_MIN_URLS = 10
# Anteil identischer Werte, ab dem wir von einem CMS-/Deploy-Zeitstempel ausgehen.
MASS_STAMP_SHARE = 0.80


def normalize_url_key(url: str) -> str:
    """Vergleichs-Schluessel fuer URLs (Sitemap-Schreibweise vs. Config/Tracking-
    Schreibweise). Ohne Schema, ohne 'www.', ohne Fragment, ohne Trailing-Slash,
    Host klein. Query bleibt erhalten (unterscheidet echte Seiten)."""
    if not url:
        return ""
    u = str(url).strip().split("#", 1)[0]
    if not u:
        return ""
    if "//" not in u:
        u = "https://" + u.lstrip("/")
    try:
        p = urlparse(u)
    except Exception:
        return u.rstrip("/").lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    key = host + path
    if p.query:
        key += "?" + p.query
    return key


def normalize_lastmod(raw) -> Optional[str]:
    """Defensives Parsen eines <lastmod>-Werts -> 'YYYY-MM-DD' oder None.

    Sitemaps liefern W3C-Datetime in allen Auspraegungen: '2026-07-30',
    '2026-07-30T12:04:11Z', '2026-07-30T12:04:11+02:00', teils mit Leerzeichen
    statt 'T'. Verworfen werden: unparsbare Werte, Datumsangaben vor
    LASTMOD_MIN_YEAR (1970-Epoche/Platzhalter) und Zukunftsdaten.
    """
    if not raw:
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(raw).strip())
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None  # z.B. 2026-02-31
    if d.year < LASTMOD_MIN_YEAR:
        return None
    today = datetime.now(timezone.utc).date()
    if (d - today).days > LASTMOD_FUTURE_TOLERANCE_DAYS:
        return None
    return d.isoformat()


def parse_sitemap_lastmods(xml_bytes: bytes) -> Dict[str, str]:
    """{normalisierte URL -> 'YYYY-MM-DD'} aus einer bereits geholten Sitemap.

    Bewusst getrennt von parse_sitemap(): dessen Rueckgabe (und damit Auswahl,
    Reihenfolge und Zahl der gecrawlten Seiten) bleibt unveraendert. URLs ohne
    <lastmod> oder mit unbrauchbarem Wert fehlen im Dict.
    """
    out: Dict[str, str] = {}
    if not xml_bytes:
        return out
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        raw = xml_bytes.decode("utf-8", errors="ignore")
        for block in re.findall(r"<url\b[^>]*>(.*?)</url>", raw,
                                flags=re.IGNORECASE | re.DOTALL)[:MAX_URLS_PER_SITEMAP]:
            mloc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.IGNORECASE | re.DOTALL)
            mlm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.IGNORECASE | re.DOTALL)
            if not (mloc and mlm):
                continue
            lm = normalize_lastmod(mlm.group(1))
            key = normalize_url_key(mloc.group(1))
            if lm and key:
                out[key] = lm
        return out
    except Exception:
        return out

    if _strip_namespace(root.tag).lower() != "urlset":
        return out
    for el in root:
        if _strip_namespace(el.tag).lower() != "url":
            continue
        loc = lastmod = None
        for child in el:
            t = _strip_namespace(child.tag).lower()
            if t == "loc" and child.text and not loc:
                loc = child.text.strip()
            elif t == "lastmod" and child.text and not lastmod:
                lastmod = child.text.strip()
        if not (loc and lastmod):
            continue
        lm = normalize_lastmod(lastmod)
        key = normalize_url_key(loc)
        if lm and key:
            out[key] = lm
        if len(out) >= MAX_URLS_PER_SITEMAP:
            break
    return out


def mass_stamp_verdict(lastmods: Iterable[str],
                       min_urls: int = MASS_STAMP_MIN_URLS,
                       share: float = MASS_STAMP_SHARE) -> Optional[Dict]:
    """Erkennt den CMS-/Deploy-Massenstempel: traegt der ueberwiegende Teil der
    URLs dasselbe <lastmod>, ist das der Zeitpunkt des letzten Deployments und
    kein Inhaltsdatum. Liefert dann {wert, anteil, n, n_gleich, grund}, sonst None.
    """
    vals = [v for v in lastmods if v]
    if len(vals) < min_urls:
        return None
    top, n_top = Counter(vals).most_common(1)[0]
    quote = n_top / len(vals)
    if quote <= share:
        return None
    return {
        "wert": top,
        "anteil": round(quote, 4),
        "n": len(vals),
        "n_gleich": n_top,
        "grund": (
            f"Massenstempel: {n_top} von {len(vals)} datierten URLs ({quote:.1%}) "
            f"tragen dasselbe lastmod {top} — das ist ein CMS-/Deploy-Zeitstempel, "
            f"kein Inhaltsdatum."
        ),
    }


# Prozessweite Sammelstelle: {normalisierte URL -> {"sitemap_lastmod", "domain",
# "gesehen_am"}}. Wird von discover_sitemap_urls beim Parsen gefuellt und von
# main.py an den Page-Tracker durchgereicht. Rein additiv — niemand muss sie lesen.
_LASTMOD_INDEX: Dict[str, Dict[str, str]] = {}
_LASTMOD_BY_DOMAIN: Dict[str, Dict[str, str]] = {}


def _record_lastmods(domain: str, mapping: Dict[str, str]) -> None:
    if not mapping:
        return
    seen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dom = (domain or "").strip().lower()
    per_dom = _LASTMOD_BY_DOMAIN.setdefault(dom, {})
    for key, lm in mapping.items():
        per_dom[key] = lm
        prev = _LASTMOD_INDEX.get(key)
        # Mehrere Sitemaps koennen dieselbe URL listen: juengsten Wert behalten.
        if prev and (prev.get("sitemap_lastmod") or "") >= lm:
            continue
        _LASTMOD_INDEX[key] = {
            "sitemap_lastmod": lm,
            "sitemap_lastmod_domain": dom,
            "sitemap_lastmod_gesehen_am": seen_at,
        }


def lastmod_index() -> Dict[str, Dict[str, str]]:
    """Alles, was in diesem Prozess an <lastmod> aus Sitemaps gesehen wurde."""
    return dict(_LASTMOD_INDEX)


def lastmod_report(domain: str) -> Dict:
    """Diagnose je Domain: wie viele URLs, wie viele mit brauchbarem lastmod,
    Wertebereich, Massenstempel-Befund. Nutzt den Sitemap-Cache, loest also
    keinen zusaetzlichen Abruf aus, wenn die Domain schon gelesen wurde."""
    urls = discover_sitemap_urls(domain)
    dom = (domain or "").strip().lower()
    per_dom = _LASTMOD_BY_DOMAIN.get(dom, {})
    hits = [per_dom[normalize_url_key(u)] for u in urls if normalize_url_key(u) in per_dom]
    vals = sorted(hits)
    return {
        "domain": domain,
        "sitemap_urls": len(urls),
        "mit_lastmod": len(hits),
        "anteil": round(len(hits) / len(urls), 4) if urls else 0.0,
        "min": vals[0] if vals else None,
        "max": vals[-1] if vals else None,
        "top5": Counter(hits).most_common(5),
        "massenstempel": mass_stamp_verdict(hits),
    }


# 20.07.2026: Prozess-Cache fuer Sitemaps.
# Grund aus dem Code-Review und zwei Timeout-Laeufen (#165, #166): Die Discovery
# lief je PRODUKT x MARKE x DOMAIN — bei 11 Produkten und rund 30 Domains also
# ~330 Sitemap-Abrufe pro Crawl. Die Sitemap einer Domain ist aber fuer alle
# Produkte dieselbe; nur der Keyword-Filter unterscheidet sich. Einmal je Domain
# holen und im Prozess behalten reduziert das auf ~30 Abrufe.
_SITEMAP_CACHE: Dict[str, List[str]] = {}


def discover_sitemap_urls(domain: str, max_depth: int = 3) -> List[str]:
    """
    Findet alle URLs, die über Sitemaps der Domain auffindbar sind.
    Verfolgt Sitemap-Indizes bis zu max_depth Ebenen.
    """
    _ck = (domain or "").strip().lower()
    if _ck in _SITEMAP_CACHE:
        return list(_SITEMAP_CACHE[_ck])
    bare = domain.rstrip("/").lstrip(".").lower()
    if bare.startswith("www."):
        host_www = bare
        host_bare = bare[4:]
    else:
        host_www = "www." + bare
        host_bare = bare

    # robots.txt beider Host-Varianten durchsuchen
    seeds: List[str] = []
    for h in (host_www, host_bare):
        seeds.extend(parse_sitemaps_from_robots(robots_txt(h)))
    _seen = set()
    uniq = []
    for s in seeds:
        if s not in _seen:
            _seen.add(s); uniq.append(s)
    seeds = uniq

    if not seeds:
        std_paths = [
            "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
            "/sitemaps.xml", "/sitemap/sitemap.xml", "/wp-sitemap.xml",
            "/sitemap1.xml", "/sitemapindex.xml",
        ]
        for h in (host_www, host_bare):
            for pth in std_paths:
                seeds.append(f"https://{h}{pth}")

    seen: Set[str] = set()
    queue: List[Tuple[str, int]] = [(u, 0) for u in seeds]
    urls: List[str] = []

    while queue and len(urls) < MAX_TOTAL_URLS:
        sm_url, depth = queue.pop(0)
        if sm_url in seen or depth > max_depth:
            continue
        seen.add(sm_url)
        xml_bytes = _fetch_sitemap(sm_url)
        if not xml_bytes:
            continue
        subs, u = parse_sitemap(xml_bytes)
        urls.extend(u)
        # <lastmod> aus denselben Bytes mitschreiben (kein zusaetzlicher Abruf).
        # Fehler hier duerfen die Discovery nie stoppen.
        try:
            _record_lastmods(_ck, parse_sitemap_lastmods(xml_bytes))
        except Exception:
            pass
        for s in subs:
            if s not in seen:
                queue.append((s, depth + 1))

    # Dedupe + Reihenfolge stabil
    seen_u: Set[str] = set()
    out: List[str] = []
    for u in urls:
        if u in seen_u:
            continue
        seen_u.add(u)
        out.append(u)
    _SITEMAP_CACHE[_ck] = list(out)
    return out


# ---------------------------------------------------------------------------
# Homepage-Fallback-Crawl (1 Hop)
# ---------------------------------------------------------------------------

def _fetch_html(url: str) -> str:
    try:
        r = requests.get(url, headers=_headers(), timeout=FETCH_TIMEOUT, allow_redirects=True)
        if r.ok and "text/html" in r.headers.get("Content-Type", "").lower():
            return r.text
    except Exception:
        pass
    return ""


def _extract_links(html: str, base: str, same_domain_only: bool = True) -> List[str]:
    out: List[str] = []
    if not html:
        return out
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base).netloc
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, href)
        if same_domain_only and urlparse(full).netloc != base_host:
            continue
        # Fragmente abschneiden
        full = full.split("#", 1)[0]
        out.append(full)
    return out


# Prozess-Cache fuer den Homepage-Crawl. Er ist der teuerste Teil der Discovery
# (bis zu MAX_CRAWL_PAGES Seitenabrufe je Aufruf) und lief bisher je Produkt neu,
# obwohl der besuchte Seitenbestand einer Domain fuer alle Produkte derselbe ist —
# nur der Keyword-Filter unterscheidet sich. Gecacht wird deshalb die MENGE der
# gefundenen URLs je Domain; gefiltert wird danach je Produkt.
_CRAWL_CACHE: Dict[str, List[str]] = {}


def discover_homepage_crawl(domain: str, keyword_regex: re.Pattern, max_pages: int = MAX_CRAWL_PAGES) -> List[str]:
    """
    Fallback, wenn keine sitemap.xml existiert oder sie blockiert ist.
    2-Hop-Crawl: Startseite + gaengige Rubriken als Seeds, dann ein Hop tiefer.
    """
    _ck = (domain or "").strip().lower()
    if _ck in _CRAWL_CACHE:
        return [u for u in _CRAWL_CACHE[_ck] if keyword_regex.search(u)]
    bare = domain.rstrip("/").lstrip(".").lower()
    host_www = bare if bare.startswith("www.") else "www." + bare
    host_bare = bare[4:] if bare.startswith("www.") else bare

    seeds: List[str] = []
    # Generische Rubriken (markenuebergreifend bekannt)
    GENERIC_RUBS = (
        "ratgeber", "magazin", "journal", "blog", "tipps", "wissen",
        "versicherung", "versicherungen", "produkte", "produkt",
        "privat", "privatkunden", "pk",
        "gesundheit", "gesundheits-tipps", "gesundheit-vorsorge-vermoegen",
        "vorsorge", "altersvorsorge", "lebensvorsorge",
        "leben", "lebensversicherung",
        "krankenversicherung", "krankenzusatzversicherung",
        "existenzsicherung", "existenzschutz",
        "vergleich", "rechner", "service",
    )
    # Produktspezifische Sub-Pfade (zahn, sterbe, risiko)
    PRODUCT_RUBS = (
        # Zahn-Welt
        "gesundheit/zahnzusatzversicherung", "gesundheit/krankenzusatzversicherung",
        "gesundheit/zahnersatz", "gesundheit/zahngesundheit", "gesundheit/zahnreinigung",
        "ratgeber/zahn", "ratgeber/zahngesundheit", "ratgeber/zahnersatz",
        "krankenversicherung/zahnzusatz", "krankenversicherung/krankenzusatz",
        "pk/gesundheit", "privatkunden/gesundheit-freizeit",
        # Sterbegeld-Welt
        "vorsorge/sterbegeldversicherung", "vorsorge/bestattungsvorsorge",
        "vorsorge/todesfallversicherung",
        "ratgeber/todesfall", "ratgeber/bestattung", "ratgeber/trauer",
        "existenzsicherung/sterbegeldversicherung",
        "pk/existenzsicherung",
        # Risikoleben-Welt
        "vorsorge/risikolebensversicherung", "vorsorge/lebensversicherung",
        "vorsorge/kapitallebensversicherung", "vorsorge/altersvorsorge",
        "ratgeber/risikolebensversicherung", "ratgeber/richtig-vorsorgen",
        "existenzsicherung/risikolebensversicherung",
        "privatkunden/vorsorge-finanzen",
        # Allgemeine Ratgeber-Hubs
        "ratgeber/gesundheit", "ratgeber/leben", "ratgeber/familie",
    )
    for h in (host_www, host_bare):
        seeds.append(f"https://{h}/")
        for rub in GENERIC_RUBS:
            seeds.append(f"https://{h}/{rub}")
        for rub in PRODUCT_RUBS:
            seeds.append(f"https://{h}/{rub}")

    queue: List[Tuple[str, int]] = [(u, 0) for u in seeds]
    seen: Set[str] = set()
    matches: List[str] = []
    # Alle besuchten Links, NICHT keyword-gefiltert. Nur diese Menge darf in den
    # Domain-Cache: Der Filter unterscheidet sich je Produkt, ein gefiltertes
    # Ergebnis wuerde spaeteren Produkten stillschweigend URLs vorenthalten.
    _all_links: List[str] = []

    while queue and len(seen) < max_pages:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        html = _fetch_html(url)
        if not html:
            continue
        for link in _extract_links(html, url, same_domain_only=True):
            _all_links.append(link)   # ungefiltert fuer den Domain-Cache
            if keyword_regex.search(link):
                matches.append(link)
            # 2-Hop: Seed-Links (depth 0) und einen weiteren Hop (depth 1) verfolgen
            elif depth < 2 and link not in seen and len(queue) < max_pages * 3:
                queue.append((link, depth + 1))
        time.sleep(0.4)

    seen_m: Set[str] = set()
    out: List[str] = []
    for u in matches:
        if u in seen_m:
            continue
        seen_m.add(u)
        out.append(u)
    _seen_all: Set[str] = set()
    _all_dedup: List[str] = []
    for u in _all_links:
        if u not in _seen_all:
            _seen_all.add(u)
            _all_dedup.append(u)
    _CRAWL_CACHE.setdefault(_ck, _all_dedup)
    return out


# ---------------------------------------------------------------------------
# Keyword-Filter
# ---------------------------------------------------------------------------

_KEYWORD_SYNONYMS: Dict[str, List[str]] = {
    # --- Zahnzusatz-Welt: Produkt + Ratgeber + Leistungen ---
    "zahnzusatz": [
        "zahnzusatz", "zahnzusatzversicherung", "zahn-zusatz",
        "zahnersatz", "zahn-ersatz", "zahnvorsorge", "zahn-vorsorge",
        "zahnversicherung", "zahn-versicherung", "zahnschutz", "zahn-schutz",
        "zahnreinigung", "zahn-reinigung", "prophylaxe",
        "zahnarzt", "zahnpflege", "zahngesundheit",
        "kieferorthopaedie", "kfo", "zahnspange",
        "inlay", "onlay", "veneer", "veneers", "bleaching",
        "zahnkrone", "zahnbruecke", "zahnimplantat", "implantologie",
        "parodontose", "parodontitis", "parodontalbehandlung",
        "wurzelbehandlung", "wurzelkanalbehandlung", "endodontie",
        "professionelle-zahnreinigung", "pzr",
        "zahnprothese", "dentallabor",
    ],
    "zahnersatz": [
        "zahnersatz", "zahnkrone", "zahnimplantat", "zahnbruecke",
        "zahnprothese", "inlay", "onlay",
    ],
    # --- Sterbegeld-Welt ---
    "sterbegeld": [
        "sterbegeld", "sterbegeldversicherung", "sterbegeld-versicherung",
        "sterbeversicherung", "sterbe-versicherung", "sterbefall",
        "bestattung", "bestattungsvorsorge", "bestattungskosten",
        "bestattungskostenversicherung", "beerdigung", "beerdigungskosten",
        "beerdigungsvorsorge", "todesfall", "todesfallversicherung-klein",
        "vorsorge-sterbegeld", "wuerdige-bestattung", "trauervorsorge",
        "beisetzung", "beisetzungskosten",
    ],
    # --- Risikoleben-Welt ---
    "risikoleben": [
        "risikoleben", "risikolebensversicherung", "risiko-lv", "risikolv",
        "risiko-lebensversicherung", "risikolebens-versicherung",
        "lebensversicherung", "lebens-versicherung",
        "todesfallversicherung", "todesfall-versicherung",
        "hinterbliebenenschutz", "hinterbliebenen-schutz",
        "familienabsicherung", "familienschutz",
        "hinterbliebenenversorgung", "einkommensschutz",
        "tilgungsabsicherung", "baukredit-absicherung",
        "absicherung-familie",
    ],
}


def _expand_keyword(kw: str) -> List[str]:
    k = kw.strip().lower()
    if not k:
        return []
    out = [k]
    for stem, syns in _KEYWORD_SYNONYMS.items():
        if stem in k or k in syns:
            for s in syns:
                if s not in out:
                    out.append(s)
    # Bindestrich-Varianten
    extra: List[str] = []
    for w in out:
        if "-" in w:
            compact = w.replace("-", "")
            if compact not in out:
                extra.append(compact)
        else:
            for suf in ("versicherung", "vorsorge", "schutz"):
                if w.endswith(suf) and len(w) > len(suf):
                    base = w[: -len(suf)]
                    cand = base.rstrip("-") + "-" + suf
                    if cand not in out and cand != w:
                        extra.append(cand)
    out.extend(extra)
    # De-dupe
    seen = set(); final = []
    for w in out:
        if w and w not in seen:
            seen.add(w); final.append(w)
    return final


def build_keyword_regex(keywords: Iterable[str]) -> re.Pattern:
    """
    Baut aus Keywords ein case-insensitives Regex. Expandiert automatisch
    bekannte Versicherungs-Synonyme + Bindestrich-Varianten.
    """
    parts: List[str] = []
    seen = set()
    for k in keywords:
        for variant in _expand_keyword(k):
            if variant in seen:
                continue
            seen.add(variant)
            escaped = re.escape(variant).replace(r"\ ", r"[\s_\-]*")
            parts.append(escaped)
    if not parts:
        return re.compile(r"$^")
    return re.compile("(" + "|".join(parts) + ")", re.IGNORECASE)


def filter_urls(urls: List[str], keyword_regex: re.Pattern) -> List[str]:
    out: List[str] = []
    for u in urls:
        if keyword_regex.search(u):
            out.append(u)
    return out


# ---------------------------------------------------------------------------
# Öffentliche Einstiegs-Funktion
# ---------------------------------------------------------------------------

def discover_for_product(
    domain: str,
    product_keywords: List[str],
    max_urls: int | None = None,
) -> Dict:
    """
    Komplette Pipeline für eine (Domain, Produkt)-Kombination.

    Liefert ein Dict mit:
      - urls: List[str], max_urls lang, de-dupliziert
      - source: "sitemap" | "crawl" | "none"
      - stats: {sitemap_total, kw_matched, crawl_visited}
    """
    if not domain:
        return {"domain": "", "urls": [], "source": "none", "stats": {}}
    rx = build_keyword_regex(product_keywords)

    sitemap_urls = discover_sitemap_urls(domain)
    sitemap_matched = [u for u in sitemap_urls if rx.search(u)]

    if len(sitemap_matched) < 5:
        crawled = discover_homepage_crawl(domain, rx)
    else:
        crawled = []

    # Merge + de-dupe
    seen: Set[str] = set()
    merged: List[str] = []
    for u in sitemap_matched + crawled:
        u = u.split("#", 1)[0].rstrip("/")
        if u not in seen:
            seen.add(u)
            merged.append(u)

    if max_urls is not None:
        merged = merged[:max_urls]

    if not merged:
        source = "none"
    else:
        if sitemap_matched and crawled:
            source = "sitemap+crawl"
        elif sitemap_matched:
            source = "sitemap"
        else:
            source = "crawl"

    return {
        "domain": domain,
        "urls": merged,
        "source": source,
        "stats": {
            "sitemap_total": len(sitemap_urls),
            "sitemap_kw_matched": len(sitemap_matched),
            "crawl_kw_matched": len(crawled),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--keywords", nargs="+", default=[])
    ap.add_argument("--max-urls", type=int, default=None)
    # Diagnose: wie viele URLs der Domain tragen ein brauchbares <lastmod>,
    # wie sehen die Werte aus, liegt ein CMS-Massenstempel vor?
    ap.add_argument("--lastmod-report", action="store_true")
    args = ap.parse_args()
    if args.lastmod_report:
        print(json.dumps(lastmod_report(args.domain), indent=2, ensure_ascii=False))
    else:
        out = discover_for_product(args.domain, args.keywords, args.max_urls)
        print(json.dumps(out, indent=2, ensure_ascii=False))
