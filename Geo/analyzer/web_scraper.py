"""
Web-Scraper für die Produktseiten.

- Holt die HTML-Seite per HTTP (mit realistischen Headers).
- Extrahiert sichtbaren Text (ohne Scripts, Styles).
- Speichert Snapshot in data/snapshots/<product_id>/<timestamp>.html
- Erstellt Content-Diff gegen den vorherigen Snapshot.

Wir verwenden requests + beautifulsoup4; Playwright wäre ideal für
JavaScript-Rendering, bläht aber den Action-Runner massiv auf. Die
ERGO-Produktseiten liefern relevanten Inhalt bereits im initialen HTML.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass
class Snapshot:
    url: str
    product_id: str
    timestamp: str
    html_path: str
    text_path: str
    text: str
    html_hash: str
    text_hash: str
    status: int
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch(url: str, timeout: int = 30) -> Dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
    }
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return {
            "status": r.status_code,
            "html": r.text if r.status_code == 200 else "",
            "latency_ms": (time.time() - t0) * 1000,
            "error": None if r.status_code == 200 else f"HTTP {r.status_code}",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": 0,
            "html": "",
            "latency_ms": (time.time() - t0) * 1000,
            "error": str(e)[:400],
        }


# ---------------------------------------------------------------------------
# Text-Extraktion
# ---------------------------------------------------------------------------

WS_RE = re.compile(r"\s+")


def extract_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    # Störende Elemente entfernen
    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()
    # Body-Text
    body = soup.body or soup
    raw = body.get_text(separator="\n")
    lines = [WS_RE.sub(" ", line).strip() for line in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Snapshot-Speicherung
# ---------------------------------------------------------------------------

def save_snapshot(
    base_dir: Path,
    product_id: str,
    url: str,
    timestamp: str,
) -> Snapshot:
    product_dir = base_dir / product_id
    product_dir.mkdir(parents=True, exist_ok=True)

    res = fetch(url)
    html = res["html"]
    text = extract_text(html)

    html_path = product_dir / f"{timestamp}.html"
    text_path = product_dir / f"{timestamp}.txt"

    if html:
        html_path.write_text(html, encoding="utf-8")
    if text:
        text_path.write_text(text, encoding="utf-8")

    return Snapshot(
        url=url,
        product_id=product_id,
        timestamp=timestamp,
        html_path=str(html_path),
        text_path=str(text_path),
        text=text,
        html_hash=_sha(html),
        text_hash=_sha(text),
        status=res["status"],
        error=res["error"],
    )


# ---------------------------------------------------------------------------
# Diff gegen letzten Snapshot
# ---------------------------------------------------------------------------

def previous_text_file(base_dir: Path, product_id: str, current_ts: str) -> Optional[Path]:
    product_dir = base_dir / product_id
    if not product_dir.exists():
        return None
    candidates = sorted(
        [p for p in product_dir.glob("*.txt") if p.stem != current_ts]
    )
    return candidates[-1] if candidates else None


def diff_against_previous(
    base_dir: Path, product_id: str, current_ts: str, current_text: str
) -> Dict:
    prev = previous_text_file(base_dir, product_id, current_ts)
    if not prev:
        return {
            "has_previous": False,
            "changed": False,
            "summary": "Erster Lauf — kein Vergleichs-Snapshot vorhanden.",
            "added_lines": [],
            "removed_lines": [],
            "similarity": 1.0,
        }

    prev_text = prev.read_text(encoding="utf-8", errors="ignore")
    if prev_text == current_text:
        return {
            "has_previous": True,
            "changed": False,
            "previous_snapshot": prev.stem,
            "summary": "Keine Veränderungen gegenüber dem letzten Snapshot.",
            "added_lines": [],
            "removed_lines": [],
            "similarity": 1.0,
        }

    prev_lines = prev_text.splitlines()
    curr_lines = current_text.splitlines()
    ratio = difflib.SequenceMatcher(a=prev_text, b=current_text).ratio()

    added, removed = [], []
    for line in difflib.unified_diff(prev_lines, curr_lines, lineterm="", n=0):
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added.append(line[1:].strip())
        elif line.startswith("-"):
            removed.append(line[1:].strip())

    added = [x for x in added if x][:100]
    removed = [x for x in removed if x][:100]

    return {
        "has_previous": True,
        "changed": True,
        "previous_snapshot": prev.stem,
        "summary": (
            f"Inhaltliche Änderungen: {len(added)} neue Zeilen, "
            f"{len(removed)} entfernte Zeilen (Ähnlichkeit {ratio:.1%})."
        ),
        "added_lines": added,
        "removed_lines": removed,
        "similarity": round(ratio, 4),
    }


# ---------------------------------------------------------------------------
# Alles zusammen
# ---------------------------------------------------------------------------

def scrape_product(base_dir: Path, product_id: str, url: str, timestamp: str) -> Dict:
    snap = save_snapshot(base_dir, product_id, url, timestamp)
    diff = diff_against_previous(base_dir, product_id, timestamp, snap.text)
    return {
        "product_id": product_id,
        "url": snap.url,
        "timestamp": snap.timestamp,
        "status": snap.status,
        "error": snap.error,
        "html_hash": snap.html_hash,
        "text_hash": snap.text_hash,
        "text_length": len(snap.text),
        "diff": diff,
    }
