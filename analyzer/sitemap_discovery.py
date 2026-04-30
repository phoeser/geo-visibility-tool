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


def discover_sitemap_urls(domain: str, max_depth: int = 3) -> List[str]:
    """
    Findet alle URLs, die über Sitemaps der Domain auffindbar sind.
    Verfolgt Sitemap-Indizes bis zu max_depth Ebenen.
    """
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


def discover_homepage_crawl(domain: str, keyword_regex: re.Pattern, max_pages: int = MAX_CRAWL_PAGES) -> List[str]:
    """
    Fallback, wenn keine sitemap.xml existiert oder sie blockiert ist.
    2-Hop-Crawl: Startseite + gaengige Rubriken als Seeds, dann ein Hop tiefer.
    """
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

    while queue and len(seen) < max_pages:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        html = _fetch_html(url)
        if not html:
            continue
        for link in _extract_links(html, url, same_domain_only=True):
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
    ap.add_argument("--keywords", nargs="+", required=True)
    ap.add_argument("--max-urls", type=int, default=None)
    args = ap.parse_args()
    out = discover_for_product(args.domain, args.keywords, args.max_urls)
    print(json.dumps(out, indent=2, ensure_ascii=False))
