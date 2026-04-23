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

import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

USER_AGENT = "geo-visibility-tool/1.0 (+https://github.com/phoeser/geo-visibility-tool)"

# Begrenze, damit ein Aufruf nicht stundenlang läuft
MAX_SITEMAP_BYTES = 8 * 1024 * 1024
MAX_URLS_PER_SITEMAP = 5000
MAX_TOTAL_URLS = 20000
MAX_CRAWL_PAGES = 40
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
        # Stream mit Hardlimit, damit wir nicht in 200-MB-Sitemaps laufen
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_SITEMAP_BYTES:
                break
        return bytes(buf)
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
    seeds: List[str] = parse_sitemaps_from_robots(robots_txt(domain))
    if not seeds:
        seeds = [
            f"https://{domain.rstrip('/')}/sitemap.xml",
            f"https://{domain.rstrip('/')}/sitemap_index.xml",
            f"https://{domain.rstrip('/')}/sitemap-index.xml",
        ]

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
    Fallback, wenn keine sitemap.xml existiert.
    1-Hop-Crawl: holt die Startseite, folgt Links, filtert nach Keywords.
    """
    start = f"https://{domain.rstrip('/')}/"
    queue: List[str] = [start]
    seen: Set[str] = set()
    matches: List[str] = []

    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        html = _fetch_html(url)
        if not html:
            continue
        for link in _extract_links(html, url, same_domain_only=True):
            if keyword_regex.search(link):
                matches.append(link)
            elif link not in seen and len(queue) < max_pages:
                queue.append(link)
        time.sleep(0.8)  # höflich

    # Dedup
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

def build_keyword_regex(keywords: Iterable[str]) -> re.Pattern:
    """
    Baut aus einer Liste von Keywords (z.B. ["zahnzusatz", "zahnersatz"])
    ein case-insensitives Regex, das sowohl in URLs als auch in Texten
    matcht. Nicht-Word-Separatoren (/, -, _, .) werden toleriert.
    """
    parts = []
    for k in keywords:
        k = k.strip().lower()
        if not k:
            continue
        # Leerzeichen im Keyword → toleranter Separator
        escaped = re.escape(k).replace(r"\ ", r"[\s_\-]*")
        parts.append(escaped)
    if not parts:
        # Matcht nichts
        return re.compile(r"$^")
    pattern = "(" + "|".join(parts) + ")"
    return re.compile(pattern, re.IGNORECASE)


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
    max_urls: int = 25,
) -> Dict:
    """
    Komplette Pipeline für eine (Domain, Produkt)-Kombination.

    Liefert ein Dict mit:
      - urls: List[str], max_urls lang, de-dupliziert
      - source: "sitemap" | "crawl" | "none"
      - stats: {sitemap_total, kw_matched, crawl_visited}
    """
    if not domain:
        return {"urls": [], "source": "none", "stats": {}}

    rx = build_keyword_regex(product_keywords)

    # Schritt 1: Sitemap-basiert
    sitemap_urls = discover_sitemap_urls(domain)
    matched = filter_urls(sitemap_urls, rx)
    if matched:
        return {
            "urls": matched[:max_urls],
            "source": "sitemap",
            "stats": {
                "sitemap_total": len(sitemap_urls),
                "kw_matched": len(matched),
                "crawl_visited": 0,
            },
        }

    # Schritt 2: Homepage-Crawl-Fallback
    crawled = discover_homepage_crawl(domain, rx)
    return {
        "urls": crawled[:max_urls],
        "source": "crawl" if crawled else "none",
        "stats": {
            "sitemap_total": len(sitemap_urls),
            "kw_matched": 0,
            "crawl_visited": MAX_CRAWL_PAGES,
        },
    }


# Kleine CLI, damit wir das Modul isoliert testen können
if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json

    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--keywords", nargs="+", required=True)
    ap.add_argument("--max-urls", type=int, default=25)
    args = ap.parse_args()
    out = discover_for_product(args.domain, args.keywords, args.max_urls)
    print(json.dumps(out, indent=2, ensure_ascii=False))
