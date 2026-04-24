"""
FlareSolverr-Fallback fuer Cloudflare-geschuetzte Seiten (Allianz & Co).

FlareSolverr ist ein selbst-gehosteter Docker-Container, der Cloudflare-
Challenges loest. Im GitHub-Actions-Workflow laeuft er als Service-Container
auf Port 8191.

API-Docs: https://github.com/FlareSolverr/FlareSolverr

ENV:
    FLARESOLVERR_URL  -> Default http://localhost:8191/v1 (im Workflow gesetzt)

Ohne FLARESOLVERR_URL oder wenn der Service nicht erreichbar ist, wird
gracefully None zurueckgeliefert und der Caller behandelt das wie einen
fehlgeschlagenen Fetch.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

import requests

DEFAULT_URL = "http://localhost:8191/v1"
FETCH_TIMEOUT = 80  # FlareSolverr braucht bis ~60s fuer Challenge-Loesung


def api_key_available() -> bool:
    """True wenn FlareSolverr konfiguriert ist."""
    return bool(os.getenv("FLARESOLVERR_URL", DEFAULT_URL).strip())


def fetch_via_api(url: str, *, render_js: bool = False,
                  premium: bool = True) -> Tuple[int, str, Optional[str]]:
    """
    Holt eine URL via FlareSolverr. render_js/premium werden zur Kompatibilitaet
    mit dem alten ScraperAPI-Interface akzeptiert, aber nicht weitergereicht -
    FlareSolverr fuehrt immer einen echten Browser aus.

    Rueckgabe: (status_code, final_url, html_or_None)
    """
    endpoint = os.getenv("FLARESOLVERR_URL", DEFAULT_URL).strip()
    if not endpoint:
        return 0, url, None
    payload = {
        "cmd": "request.get",
        "url": url,
        "maxTimeout": 60000,
    }
    try:
        r = requests.post(endpoint, json=payload, timeout=FETCH_TIMEOUT)
        if r.status_code != 200:
            return r.status_code, url, None
        data = r.json()
        if data.get("status") != "ok":
            # FlareSolverr konnte Cloudflare nicht loesen
            return 502, url, None
        solution = data.get("solution") or {}
        html = solution.get("response") or ""
        final_url = solution.get("url") or url
        http_status = int(solution.get("status") or 200)
        if not html:
            return http_status, final_url, None
        return http_status, final_url, html
    except Exception as e:  # noqa: BLE001
        print(f"[FLARESOLVERR] Error fuer {url}: {e}")
        return 0, url, None


def looks_like_cloudflare_challenge(html: str) -> bool:
    """Erkennt Cloudflare-Bot-Challenge-Seiten im HTML."""
    if not html:
        return False
    low = html.lower()
    signals = [
        "just a moment...",
        "enable javascript and cookies to continue",
        "/cdn-cgi/challenge-platform",
        "cf-challenge",
        "cf_chl_opt",
    ]
    return any(s in low for s in signals)
