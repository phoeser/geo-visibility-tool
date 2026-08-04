"""
Aufloeser fuer Weiterleitungs-URLs in LLM-Quellenangaben (04.08.2026).

Hintergrund
-----------
Gemini liefert seine Grounding-Quellen nicht als echte URL, sondern als
Weiterleitung ueber `vertexaisearch.cloud.google.com/grounding-api-redirect/...`.
Im Lauf 2026-08-04 waren das 4.659 von 4.731 Quellen. Damit ist zwar belegt,
DASS gesucht wurde, aber nicht, WAS zitiert wurde: `metrics.domain_of()` sieht
bei jeder dieser Quellen nur `vertexaisearch.cloud.google.com`.

Was dieses Modul macht
----------------------
Ein einzelner HEAD-Request OHNE Redirect-Folgen. Google antwortet mit 302 und
setzt `Location` auf die Ziel-URL — die Zielseite selbst wird dabei nie
kontaktiert. Gemessen an 25 echten URLs aus dem Lauf vom 04.08.2026:
25/25 aufgeloest, Median 0,067 s, Mittel 0,095 s, Maximum 0,61 s (mit
Keep-Alive-Session). Der Vollabruf MIT Redirect-Folgen war 20x langsamer
(Median 1,44 s) und lieferte bei 3 von 15 Zielen 400/403, weil manche Server
HEAD ablehnen — fuer die Ziel-URL brauchen wir sie aber gar nicht.

Budget
------
Auch bei 67 ms bleibt es ein Netzwerkaufruf je Quelle. Deshalb harte Grenzen,
die im Zweifel zu unvollstaendigen, aber ehrlich markierten Daten fuehren:
  - `max_resolutions`  Stueckgrenze je Prozess (Default 6000)
  - `budget_seconds`   Wanduhr-Frist ab der ersten Aufloesung (Default 900 s)
  - `timeout`          je Request (Default 4 s)
  - `max_hops`         Weiterleitungsketten laenger als 3 werden abgebrochen
Wird eine Grenze gerissen, liefert der Resolver `resolve_status="budget"` und
laesst `url_resolved` leer. Die urspruengliche `url` bleibt immer erhalten.

Cache: prozessweit, thread-sicher. Dieselbe URI wird nie zweimal abgerufen.
(Im Lauf vom 04.08. waren 4.731 Quellen nur 4.666 verschiedene URIs — der
Cache spart also wenig, kostet aber auch nichts.)

Der Resolver wirft NIE. Jeder Fehler wird zu einem resolve_status.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

# Hosts, deren URLs ueberhaupt aufgeloest werden. Alles andere ist bereits eine
# echte URL und wird nicht angefasst (kein unnoetiger Traffic auf Fremdseiten).
REDIRECT_HOSTS = (
    "vertexaisearch.cloud.google.com",
    "grounding-api-redirect.googleapis.com",
)

# resolve_status-Werte (bewusst deutsch, wie die uebrigen Statusfelder im Projekt)
ST_OK = "ok"              # aufgeloest, url_resolved gesetzt
ST_DIREKT = "direkt"      # war keine Weiterleitung, url == url_resolved
ST_BUDGET = "budget"      # Budget/Stueckgrenze erschoepft, nicht versucht
ST_AUS = "aus"            # Aufloesung per Schalter deaktiviert
ST_FEHLER = "fehler"      # Netzwerk-/HTTP-Fehler
ST_KETTE = "kette_zu_lang"  # mehr Spruenge als erlaubt


def domain_of_url(url: str) -> str:
    """Host ohne fuehrendes www. Leerstring, wenn nicht parsebar."""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def is_redirect_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower() if url else ""
    return any(host == h or host.endswith("." + h) for h in REDIRECT_HOSTS)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        print(f"[WARN] {name}={raw!r} ist keine Zahl - nutze {default}")
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in ("0", "false", "off", "nein", "no")


class RedirectResolver:
    """Loest Weiterleitungs-URLs auf. Thread-sicher, gecacht, budgetiert."""

    def __init__(self, timeout: float = 4.0, max_hops: int = 3,
                 max_resolutions: int = 6000, budget_seconds: float = 900.0,
                 enabled: bool = True):
        self.timeout = timeout
        self.max_hops = max_hops
        self.max_resolutions = max_resolutions
        self.budget_seconds = budget_seconds
        self.enabled = enabled
        self._cache: Dict[str, Dict[str, str]] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self._count = 0
        self._deadline: Optional[float] = None
        self._budget_gemeldet = False

    # -- interne Helfer ---------------------------------------------------

    def _session(self) -> requests.Session:
        """Je Thread eine Session (Keep-Alive spart den TLS-Handshake:
        0,61 s beim ersten Aufruf gegen 0,07 s bei den folgenden)."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": "geo-visibility-tool/1.0 (+resolver)"})
            self._local.session = s
        return s

    def _budget_frei(self) -> bool:
        """Reserviert eine Aufloesung, wenn noch Budget da ist."""
        with self._lock:
            now = time.time()
            if self._deadline is None:
                self._deadline = now + self.budget_seconds
            if self._count >= self.max_resolutions or now >= self._deadline:
                if not self._budget_gemeldet:
                    self._budget_gemeldet = True
                    print(f"[RESOLVE] Budget erschoepft nach {self._count} Aufloesungen "
                          f"({self.budget_seconds:.0f}s / max {self.max_resolutions}). "
                          f"Restliche Quellen bleiben mit resolve_status='budget' unaufgeloest.")
                return False
            self._count += 1
            return True

    def _resolve_uncached(self, url: str) -> Dict[str, str]:
        cur = url
        for _ in range(self.max_hops):
            try:
                r = self._session().head(cur, allow_redirects=False, timeout=self.timeout)
            except Exception as e:  # noqa: BLE001
                return {"url_resolved": "", "domain": "",
                        "resolve_status": f"{ST_FEHLER}:{type(e).__name__}"}
            loc = r.headers.get("Location") or r.headers.get("location")
            if 300 <= r.status_code < 400 and loc:
                cur = urljoin(cur, loc)
                if not is_redirect_url(cur):
                    # Ziel erreicht. Der Zielserver wird bewusst NICHT kontaktiert.
                    return {"url_resolved": cur, "domain": domain_of_url(cur),
                            "resolve_status": ST_OK}
                continue
            if cur != url:
                return {"url_resolved": cur, "domain": domain_of_url(cur),
                        "resolve_status": ST_OK}
            return {"url_resolved": "", "domain": "",
                    "resolve_status": f"{ST_FEHLER}:http{r.status_code}"}
        return {"url_resolved": "", "domain": "", "resolve_status": ST_KETTE}

    # -- oeffentliche API -------------------------------------------------

    def resolve(self, url: str) -> Dict[str, str]:
        """Liefert immer ein Dict mit url_resolved/domain/resolve_status.
        Wirft nie."""
        if not url:
            return {"url_resolved": "", "domain": "", "resolve_status": ST_FEHLER}
        if not is_redirect_url(url):
            return {"url_resolved": url, "domain": domain_of_url(url),
                    "resolve_status": ST_DIREKT}
        if not self.enabled:
            return {"url_resolved": "", "domain": "", "resolve_status": ST_AUS}
        with self._lock:
            hit = self._cache.get(url)
        if hit is not None:
            return dict(hit)
        if not self._budget_frei():
            # bewusst NICHT cachen: ein spaeterer Lauf soll es erneut versuchen
            return {"url_resolved": "", "domain": "", "resolve_status": ST_BUDGET}
        try:
            res = self._resolve_uncached(url)
        except Exception as e:  # noqa: BLE001  (Guertel und Hosentraeger)
            res = {"url_resolved": "", "domain": "",
                   "resolve_status": f"{ST_FEHLER}:{type(e).__name__}"}
        with self._lock:
            self._cache[url] = res
        return dict(res)

    def annotate_sources(self, sources: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Ergaenzt url_resolved/domain/resolve_status IN PLACE-frei (neue Dicts).
        `url` bleibt unveraendert. Faellt die Aufloesung aus, wird `domain` aus
        einem bereits vorhandenen Domain-Feld (z. B. Geminis chunk.web.domain
        bzw. dessen title) uebernommen, damit wenigstens die Domain-Ebene
        auswertbar bleibt."""
        out: List[Dict[str, str]] = []
        for s in sources or []:
            try:
                neu = dict(s)
                res = self.resolve(neu.get("url", ""))
                neu["url_resolved"] = res["url_resolved"]
                neu["resolve_status"] = res["resolve_status"]
                # Vorbelegte Domain (aus den Chunk-Metadaten) nicht ueberschreiben,
                # wenn die Aufloesung nichts geliefert hat.
                if res.get("domain"):
                    neu["domain"] = res["domain"]
                else:
                    neu.setdefault("domain", "")
                out.append(neu)
            except Exception:  # noqa: BLE001
                out.append(dict(s))
        return out

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"aufgeloest": self._count, "cache": len(self._cache)}


_default_resolver: Optional[RedirectResolver] = None
_default_lock = threading.Lock()


def get_resolver() -> RedirectResolver:
    """Prozessweiter Resolver, konfiguriert ueber Umgebungsvariablen:

      GEO_RESOLVE_REDIRECTS   0/off schaltet die Aufloesung ab (Default an)
      GEO_RESOLVE_MAX         Stueckgrenze je Lauf (Default 6000)
      GEO_RESOLVE_BUDGET_S    Wanduhr-Frist in Sekunden (Default 900)
      GEO_RESOLVE_TIMEOUT_S   Timeout je Request (Default 4)
    """
    global _default_resolver
    if _default_resolver is None:
        with _default_lock:
            if _default_resolver is None:
                _default_resolver = RedirectResolver(
                    timeout=float(_env_int("GEO_RESOLVE_TIMEOUT_S", 4)),
                    max_resolutions=_env_int("GEO_RESOLVE_MAX", 6000),
                    budget_seconds=float(_env_int("GEO_RESOLVE_BUDGET_S", 900)),
                    enabled=_env_flag("GEO_RESOLVE_REDIRECTS", True),
                )
    return _default_resolver
