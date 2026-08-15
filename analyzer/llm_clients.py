"""
LLM-Clients für Gemini (Google) und Claude (Anthropic).

Beide Clients exponieren dieselbe Methode `ask(prompt)` und liefern ein
einheitliches Response-Schema:

{
    "text":     "<Antworttext>",
    "sources":  [ {"title": "...", "url": "..."}, ... ],   # falls verfügbar
    "model":    "<modell-id>",
    # 04.08.2026, additiv je Quelle (alte Felder unveraendert):
    #   "src_typ":        "annotation" = vom Anbieter als Zitat geliefert
    #                     "fliesstext" = aus dem Antworttext geraten
    #   "domain"          Domain der Quelle, wenn ermittelbar
    #   "url_resolved"    aufgeloeste Ziel-URL bei Weiterleitungen (Gemini)
    #   "resolve_status"  ok | direkt | budget | aus | fehler:* | kette_zu_lang
    "latency_ms": <float>,
    "tokens_in": <int | None>,
    "tokens_out": <int | None>,
    "error":    "<str | None>"
}

Fehler werden nicht geworfen, sondern als Error-Feld zurückgegeben, damit
ein einzelner fehlschlagender Request den gesamten Lauf nicht killt.
"""

from __future__ import annotations

import os
import time
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

from analyzer.redirect_resolver import get_resolver, domain_of_url, _env_flag


# --- Herkunft einer Quelle (04.08.2026) ------------------------------------
# "annotation"  = der Anbieter hat die URL selbst als Zitat ausgewiesen
#                 (Gemini groundingChunks, OpenAI url_citation, Perplexity
#                 citations). Belegt.
# "fliesstext"  = wir haben die URL per Regex aus dem Antworttext gefischt.
#                 Das Modell KANN sie erfunden haben. Nicht belegt.
SRC_ANNOTATION = "annotation"
SRC_FLIESSTEXT = "fliesstext"


# --- System-Prompt: gleiche Instruktion an beide LLMs -----------------------

SYSTEM_PROMPT = (
    "Du bist ein hilfreicher Assistent, der Versicherungsfragen beantwortet. "
    "Gib konkrete Anbieter- und Produktnamen an, wenn nach Empfehlungen oder "
    "Vergleichen gefragt wird. Wenn du Quellen nutzt, gib sie als URLs am Ende "
    "deiner Antwort unter der Überschrift 'Quellen:' an. Antworte auf Deutsch."
)


@dataclass
class LLMResponse:
    text: str
    sources: List[Dict[str, str]]
    model: str
    latency_ms: float
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "text": self.text,
            "sources": self.sources,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "error": self.error,
        }


# --- Quellen-Extraktion aus Plaintext-Antworten -----------------------------

URL_REGEX = re.compile(r"https?://[^\s\)\]\>\"]+", re.IGNORECASE)


def extract_urls_from_text(text: str) -> List[Dict[str, str]]:
    """Fallback: URLs aus dem Antwort-Text ziehen."""
    urls = URL_REGEX.findall(text)
    # Duplikate entfernen, Reihenfolge erhalten
    seen = set()
    out = []
    for u in urls:
        clean = u.rstrip(".,;:")
        if clean not in seen:
            seen.add(clean)
            # src_typ/domain sind additiv (04.08.2026). Die Zaehlung in
            # metrics.py liest weiterhin nur "url" — an den Messwerten der
            # bestehenden Engines aendert das nichts.
            out.append({"title": "", "url": clean,
                        "src_typ": SRC_FLIESSTEXT,
                        "domain": domain_of_url(clean)})
    return out


_DOMAIN_REGEX = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)


def _sieht_aus_wie_domain(s: str) -> bool:
    """True, wenn der String eine nackte Domain ist ("allianz.de"), nicht ein
    Seitentitel ("Zahnzusatzversicherung | Allianz")."""
    s = (s or "").strip().lower()
    if not s or len(s) > 253 or " " in s:
        return False
    if s.startswith("www."):
        s = s[4:]
    return bool(_DOMAIN_REGEX.match(s))


# --- Retry-Wrapper ----------------------------------------------------------

def _endgueltiger_fehler(e: Exception) -> bool:
    """401/403: Auth oder Guthaben. Der zweite und dritte Versuch koennen daran
    nichts aendern - dieselben Zugangsdaten, dasselbe leere Konto.

    15.08.2026, Befund aus dem Lauf vom 13.08.: Perplexity antwortete auf alle
    60 SOHO-Prompts mit 401 "exceeded your current quota", und jeder davon
    wurde brav dreimal probiert, mit Backoff dazwischen. Ueber alle 390
    Prompts eines vollen Laufs sind das ~780 aussichtslose Wiederholungen und
    rund 38 Minuten reine Wartezeit - pro Lauf, solange das Guthaben leer ist.
    Die Erkennung ist bewusst stumpf (Stringsuche im Fehlertext), weil die
    Clients hier requests-, openai- und google-Exceptions durchreichen und es
    keinen gemeinsamen Statuscode-Zugriff gibt."""
    s = str(e)
    return ("401" in s or "403" in s or "insufficient_quota" in s
            or "exceeded your current quota" in s.lower())


def with_retries(func, attempts: int = 3, base_delay: float = 2.0):
    """Exponentielles Backoff bei Fehlern. 401/403 sofort endgueltig."""
    last_err = None
    for i in range(attempts):
        try:
            return func()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if _endgueltiger_fehler(e):
                break
            # 20.07.2026 Review-Fix: Hier wurde AUCH nach dem letzten Versuch
            # geschlafen. Bei attempts=3 also 2+4+8 = 14 s je endgueltig
            # gescheitertem Call statt der im Kommentar behaupteten 6 s — bei einer
            # toten Engine mal 366 Prompts rund 85 Minuten reine Wartezeit.
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i)
            time.sleep(delay)
    raise last_err  # type: ignore[misc]


# ============================================================================
# Claude (Anthropic)
# ============================================================================

class ClaudeClient:
    """Ruft Claude über die Anthropic Messages API auf."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6",
                 max_tokens: int = 1200, temperature: float = 0.3,
                 retries: int = 3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._retries = retries   # 17.07.2026: war 6x hartkodiert als attempts=3, s. build_clients()
        self.url = "https://api.anthropic.com/v1/messages"

    def ask(self, prompt: str) -> LLMResponse:
        def _call():
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            }
            t0 = time.time()
            r = requests.post(self.url, json=payload, headers=headers, timeout=90)
            latency = (time.time() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError(f"Claude HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            text = "".join(
                block.get("text", "") for block in data.get("content", [])
                if block.get("type") == "text"
            )
            usage = data.get("usage", {}) or {}
            return LLMResponse(
                text=text,
                sources=extract_urls_from_text(text),
                model=self.model,
                latency_ms=latency,
                tokens_in=usage.get("input_tokens"),
                tokens_out=usage.get("output_tokens"),
            )

        try:
            return with_retries(_call, attempts=self._retries)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(
                text="", sources=[], model=self.model,
                latency_ms=0.0, error=str(e)[:500],
            )


# ============================================================================
# Gemini (Google AI Studio)
# ============================================================================

class GeminiClient:
    """Ruft Gemini über die Google AI Studio REST-API auf."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash",
                 max_tokens: int = 1200, temperature: float = 0.3,
                 retries: int = 3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._retries = retries   # 17.07.2026: war 6x hartkodiert als attempts=3, s. build_clients()
        self.url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{model}:generateContent?key={api_key}"
        )

    def ask(self, prompt: str) -> LLMResponse:
        def _call():
            payload = {
                "systemInstruction": {
                    "parts": [{"text": SYSTEM_PROMPT}]
                },
                "contents": [
                    {"role": "user", "parts": [{"text": prompt}]}
                ],
                "generationConfig": {
                    "temperature": self.temperature,
                    "maxOutputTokens": 2048, "thinkingConfig": {"thinkingBudget": 0},
                },
                # Grounding mit Google Search aktivieren, um Quellen zu bekommen:
                "tools": [{"googleSearch": {}}],
            }
            headers = {"content-type": "application/json"}
            t0 = time.time()
            r = requests.post(self.url, json=payload, headers=headers, timeout=90)
            latency = (time.time() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()

            # Textinhalte extrahieren
            candidates = data.get("candidates", [])
            if not candidates:
                raise RuntimeError(f"Gemini leere Candidates: {data}")
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)

            # Quellen aus Grounding-Metadata (falls Search verwendet wurde)
            #
            # 04.08.2026: Bis heute stand hier nur title + uri. Die uri ist bei
            # Gemini aber fast immer eine Weiterleitung ueber
            # vertexaisearch.cloud.google.com (Lauf 04.08.: 4.659 von 4.731).
            # metrics.domain_of() sah dadurch bei praktisch jeder Gemini-Quelle
            # nur "vertexaisearch.cloud.google.com" statt der zitierten Seite.
            # Zwei additive Ergaenzungen:
            #   1. domain aus dem Chunk selbst (chunk.web.domain; faellt das Feld
            #      weg, steht die Domain bei dieser API-Version im title —
            #      geprueft an 4.731 Quellen: title war dort immer die nackte
            #      Domain, z. B. "allianz.de"). Kostet nichts.
            #   2. url_resolved per HEAD ohne Redirect-Folgen, budgetiert
            #      (siehe analyzer/redirect_resolver.py).
            # `url` bleibt unveraendert, damit keine Historie bricht.
            sources: List[Dict[str, str]] = []
            ground = candidates[0].get("groundingMetadata", {}) or {}
            for chunk in ground.get("groundingChunks", []) or []:
                web = chunk.get("web", {}) or {}
                if web.get("uri"):
                    titel = web.get("title", "") or ""
                    dom = (web.get("domain") or "").strip().lower()
                    if dom.startswith("www."):
                        dom = dom[4:]
                    if not dom and _sieht_aus_wie_domain(titel):
                        dom = titel.strip().lower()
                    sources.append({
                        "title": titel,
                        "url": web.get("uri", ""),
                        "src_typ": SRC_ANNOTATION,
                        "domain": dom,
                    })
            if sources:
                # Weiterleitungen aufloesen. Wirft nie, respektiert das Budget
                # und laesst die vorbelegte Chunk-Domain stehen, wenn die
                # Aufloesung ausfaellt.
                sources = get_resolver().annotate_sources(sources)
            # Fallback: URLs direkt aus Text
            if not sources:
                sources = extract_urls_from_text(text)

            usage = data.get("usageMetadata", {}) or {}
            return LLMResponse(
                text=text,
                sources=sources,
                model=self.model,
                latency_ms=latency,
                tokens_in=usage.get("promptTokenCount"),
                tokens_out=usage.get("candidatesTokenCount"),
            )

        try:
            return with_retries(_call, attempts=self._retries)
        except Exception as e:  # noqa: BLE001
            # Falls Grounding den Fehler verursacht, einmal ohne versuchen
            msg = str(e)
            if "googleSearch" in msg or "tools" in msg or "grounding" in msg.lower():
                try:
                    return self._ask_without_grounding(prompt)
                except Exception as e2:  # noqa: BLE001
                    return LLMResponse(
                        text="", sources=[], model=self.model,
                        latency_ms=0.0, error=str(e2)[:500],
                    )
            return LLMResponse(
                text="", sources=[], model=self.model,
                latency_ms=0.0, error=msg[:500],
            )

    def _ask_without_grounding(self, prompt: str) -> LLMResponse:
        payload = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": 2048, "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        t0 = time.time()
        r = requests.post(self.url, json=payload, timeout=90)
        latency = (time.time() - t0) * 1000
        r.raise_for_status()
        data = r.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        usage = data.get("usageMetadata", {}) or {}
        return LLMResponse(
            text=text,
            sources=extract_urls_from_text(text),
            model=self.model,
            latency_ms=latency,
            tokens_in=usage.get("promptTokenCount"),
            tokens_out=usage.get("candidatesTokenCount"),
        )



# ============================================================================
# OpenAI (ChatGPT)
# ============================================================================

class OpenAIClient:
    """Ruft OpenAI ChatGPT ueber die Chat Completions API auf."""

    def __init__(self, api_key: str, model: str = "gpt-4o-mini",
                 max_tokens: int = 1200, temperature: float = 0.3,
                 retries: int = 3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._retries = retries   # 17.07.2026: war 6x hartkodiert als attempts=3, s. build_clients()
        self.url = "https://api.openai.com/v1/chat/completions"

    def ask(self, prompt: str) -> LLMResponse:
        def _call():
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            }
            t0 = time.time()
            r = requests.post(self.url, json=payload, headers=headers, timeout=90)
            latency = (time.time() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError(f"OpenAI HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"OpenAI leere Choices: {data}")
            msg = (choices[0].get("message") or {})
            text = msg.get("content") or ""
            usage = data.get("usage", {}) or {}
            return LLMResponse(
                text=text,
                sources=extract_urls_from_text(text),
                model=self.model,
                latency_ms=latency,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
            )

        try:
            return with_retries(_call, attempts=self._retries)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(
                text="", sources=[], model=self.model,
                latency_ms=0.0, error=str(e)[:500],
            )



# ============================================================================
# OpenAI mit Websuche  (Engine-ID: chatgpt_web)   -   04.08.2026
# ============================================================================
#
# WARUM EINE ZWEITE ENGINE STATT EINER OPTION AN DER BESTEHENDEN
# ---------------------------------------------------------------
# `chatgpt` (gpt-4o-mini, /v1/chat/completions, ohne tools) antwortet aus dem
# Modellgedaechtnis. Die dort gezaehlten "sources" sind ausschliesslich per
# Regex aus dem Fliesstext gefischte URLs — im Lauf 04.08.2026 waren das 1.582
# Stueck, von denen keine einzige vom Modell als Zitat ausgewiesen war.
# Diese Engine bleibt unveraendert: gleiches Modell, gleiche Payload, gleiche
# Zaehlung. Sonst braeche die SoV-Zeitreihe.
# `chatgpt_web` ist ein ZUSAETZLICHER Kanal mit aktivierter Websuche. Erst der
# Vergleich beider Kanaele zeigt, was Websuche an der Sichtbarkeit aendert.
#
# WELCHE API-VARIANTE
# -------------------
# Geprueft an der OpenAI-Doku (Stand 04.08.2026):
#   - /v1/responses  + tools:[{"type":"web_search"}]
#       unterstuetzte Modelle: gpt-5.6, gpt-5.5, gpt-4.1, gpt-4.1-mini.
#       gpt-4o-mini — das Modell dieses Projekts — ist NICHT dabei.
#   - /v1/chat/completions + web_search_options
#       nur mit den Such-Modellvarianten: gpt-4o-mini-search-preview,
#       gpt-4o-search-preview (deprecated), gpt-5-search-api.
# Default ist deshalb gpt-4o-mini-search-preview ueber Chat Completions: gleiche
# 4o-mini-Basis wie der bestehende Kanal, also ist der Unterschied zwischen
# beiden Zeitreihen tatsaechlich die Websuche und nicht ein Modellwechsel.
# Wer lieber die Responses-API will, setzt in config.json api:"responses" und
# ein dort unterstuetztes Modell (z. B. gpt-4.1-mini). Beide Pfade sind
# implementiert; `api:"auto"` waehlt nach dem Modellnamen.
#
# BEKANNTE EIGENHEITEN DER SUCH-MODELLE
# -------------------------------------
#   - `temperature` wird von den *-search-preview-Modellen abgelehnt. Wird
#     deshalb im Chat-Pfad NICHT mitgeschickt.
#   - Ob gesucht wird, entscheidet das Modell. Antworten ohne Suche sind
#     moeglich und ein valides Ergebnis (dann: keine annotations).

OPENAI_WEB_SEARCH_TOOL = {"type": "web_search"}


def _openai_annotation_sources(annotations) -> List[Dict[str, str]]:
    """Zieht echte Zitate aus annotations[].

    Zwei Schreibweisen kommen in freier Wildbahn vor und werden beide
    unterstuetzt:
      Responses-API (flach):  {"type":"url_citation","url":...,"title":...}
      Chat-Completions:       {"type":"url_citation","url_citation":{"url":...}}
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for a in annotations or []:
        if not isinstance(a, dict):
            continue
        if a.get("type") not in (None, "url_citation"):
            continue
        inner = a.get("url_citation")
        node = inner if isinstance(inner, dict) else a
        url = node.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({
            "title": (node.get("title") or "")[:300],
            "url": url,
            "src_typ": SRC_ANNOTATION,
            "domain": domain_of_url(url),
        })
    return out


class OpenAIWebSearchClient:
    """ChatGPT MIT Websuche. Quellen kommen aus den url_citation-Annotationen,
    nicht aus dem Fliesstext."""

    CHAT_URL = "https://api.openai.com/v1/chat/completions"
    RESPONSES_URL = "https://api.openai.com/v1/responses"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini-search-preview",
                 max_tokens: int = 1200, temperature: float = 0.3,
                 retries: int = 3, api: str = "auto",
                 search_context_size: str = "low"):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._retries = retries
        # Kosten: kleinste Suchtiefe als Default, wie bei Perplexity.
        self.search_context_size = search_context_size or "low"
        if api not in ("auto", "chat", "responses"):
            print(f"[WARN] chatgpt_web: api={api!r} unbekannt - nutze 'auto'")
            api = "auto"
        if api == "auto":
            api = "chat" if "search" in (model or "").lower() else "responses"
        self.api = api

    # -- Chat-Completions-Pfad (Such-Modellvarianten) ----------------------

    def _call_chat(self, prompt: str) -> Dict:
        return {
            "url": self.CHAT_URL,
            "payload": {
                "model": self.model,
                "max_tokens": self.max_tokens,
                # temperature bewusst weggelassen: die *-search-preview-Modelle
                # lehnen den Parameter mit HTTP 400 ab.
                "web_search_options": {"search_context_size": self.search_context_size},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            },
        }

    @staticmethod
    def _parse_chat(data: Dict):
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"OpenAI-Web leere Choices: {str(data)[:300]}")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        sources = _openai_annotation_sources(msg.get("annotations"))
        usage = data.get("usage", {}) or {}
        return text, sources, usage.get("prompt_tokens"), usage.get("completion_tokens")

    # -- Responses-Pfad (tools:[{"type":"web_search"}]) --------------------

    def _call_responses(self, prompt: str) -> Dict:
        return {
            "url": self.RESPONSES_URL,
            "payload": {
                "model": self.model,
                "max_output_tokens": self.max_tokens,
                "temperature": self.temperature,
                "tools": [OPENAI_WEB_SEARCH_TOOL],
                "instructions": SYSTEM_PROMPT,
                "input": prompt,
            },
        }

    @staticmethod
    def _parse_responses(data: Dict):
        text_teile: List[str] = []
        annos: List[Dict] = []
        for item in data.get("output") or []:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue  # web_search_call-Eintraege enthalten keinen Text
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in ("output_text", "text"):
                    if part.get("text"):
                        text_teile.append(part["text"])
                    annos.extend(part.get("annotations") or [])
        text = "".join(text_teile)
        if not text and isinstance(data.get("output_text"), str):
            text = data["output_text"]  # Bequemlichkeitsfeld mancher Versionen
        usage = data.get("usage", {}) or {}
        return (text, _openai_annotation_sources(annos),
                usage.get("input_tokens"), usage.get("output_tokens"))

    # -- gemeinsamer Aufruf ------------------------------------------------

    def ask(self, prompt: str) -> LLMResponse:
        # prompt bewusst als Argument durchgereicht und NICHT auf self
        # zwischengespeichert: main.py teilt eine Client-Instanz auf
        # parallel_requests Threads auf (ThreadPoolExecutor). Ein Feld auf self
        # waere ein Datenrennen und wuerde Antworten den falschen Prompts
        # zuordnen.
        def _call():
            spec = (self._call_chat(prompt) if self.api == "chat"
                    else self._call_responses(prompt))
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            t0 = time.time()
            # Websuche dauert laenger als eine reine Modellantwort, deshalb
            # 120 s statt der sonst ueblichen 90 s.
            r = requests.post(spec["url"], json=spec["payload"],
                              headers=headers, timeout=120)
            latency = (time.time() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError(
                    f"OpenAI-Web ({self.api}) HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            if self.api == "chat":
                text, sources, tin, tout = self._parse_chat(data)
            else:
                text, sources, tin, tout = self._parse_responses(data)
            # Kein Fallback auf extract_urls_from_text, wenn Annotationen da
            # sind. Fehlen sie ganz (Modell hat nicht gesucht), nehmen wir die
            # Fliesstext-URLs — aber als solche markiert, damit spaeter
            # unterscheidbar bleibt, was belegt ist.
            if not sources and text:
                sources = extract_urls_from_text(text)
            return LLMResponse(text=text, sources=sources, model=self.model,
                               latency_ms=latency, tokens_in=tin, tokens_out=tout)

        try:
            return with_retries(_call, attempts=self._retries)
        except Exception as e:  # noqa: BLE001
            # Der Lauf darf an dieser Engine NIE scheitern: leere Antwort +
            # Fehlertext, alles andere laeuft weiter.
            return LLMResponse(text="", sources=[], model=self.model,
                               latency_ms=0.0, error=str(e)[:500])


# ============================================================================
# Grok (xAI)  -  OpenAI-kompatibles Chat-Completions-Format
# ============================================================================

class GrokClient:
    """Ruft xAI Grok ueber die Chat-Completions-API auf."""

    def __init__(self, api_key: str, model: str = "grok-2-1212",
                 max_tokens: int = 1200, temperature: float = 0.3,
                 retries: int = 3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._retries = retries   # 17.07.2026: war 6x hartkodiert als attempts=3, s. build_clients()
        self.url = "https://api.x.ai/v1/chat/completions"

    def ask(self, prompt: str) -> LLMResponse:
        def _call():
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            }
            t0 = time.time()
            r = requests.post(self.url, json=payload, headers=headers, timeout=90)
            latency = (time.time() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError(f"Grok HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"Grok leere Choices: {data}")
            text = (choices[0].get("message") or {}).get("content") or ""
            usage = data.get("usage", {}) or {}
            return LLMResponse(
                text=text,
                sources=extract_urls_from_text(text),
                model=self.model,
                latency_ms=latency,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
            )
        try:
            return with_retries(_call, attempts=self._retries)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(text="", sources=[], model=self.model,
                               latency_ms=0.0, error=str(e)[:500])


# ============================================================================
# Perplexity (Sonar)  -  OpenAI-kompatibel mit eingebauter Web-Suche
# ============================================================================

class PerplexityClient:
    """Ruft Perplexity Sonar ueber Chat-Completions auf. Web-Suche integriert."""

    def __init__(self, api_key: str, model: str = "sonar",
                 max_tokens: int = 1200, temperature: float = 0.3,
                 retries: int = 3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._retries = retries   # 17.07.2026: war 6x hartkodiert als attempts=3, s. build_clients()
        self.url = "https://api.perplexity.ai/chat/completions"

    def ask(self, prompt: str) -> LLMResponse:
        def _call():
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                # Kosten: guenstigste Suchtiefe explizit setzen (billigste Gebuehrenstufe)
                "web_search_options": {"search_context_size": "low"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            }
            t0 = time.time()
            r = requests.post(self.url, json=payload, headers=headers, timeout=90)
            latency = (time.time() - t0) * 1000
            if r.status_code != 200:
                raise RuntimeError(f"Perplexity HTTP {r.status_code}: {r.text[:400]}")
            data = r.json()
            choices = data.get("choices", [])
            if not choices:
                raise RuntimeError(f"Perplexity leere Choices: {data}")
            text = (choices[0].get("message") or {}).get("content") or ""
            # Perplexity liefert citations als Liste von URLs auf top-level
            cits = data.get("citations") or []
            sources = []
            seen = set()
            for c in cits:
                if isinstance(c, str) and c not in seen:
                    seen.add(c)
                    # src_typ/domain additiv (04.08.2026), Zaehlung unveraendert
                    sources.append({"title": "", "url": c,
                                    "src_typ": SRC_ANNOTATION,
                                    "domain": domain_of_url(c)})
            if not sources:
                sources = extract_urls_from_text(text)
            usage = data.get("usage", {}) or {}
            return LLMResponse(
                text=text,
                sources=sources,
                model=self.model,
                latency_ms=latency,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
            )
        try:
            return with_retries(_call, attempts=self._retries)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(text="", sources=[], model=self.model,
                               latency_ms=0.0, error=str(e)[:500])


class SerpApiGoogleClient:
    """
    Google **AI Overview** bzw. **AI Mode** ueber SerpApi.

    Warum SerpApi: Google bietet fuer AI Overview / AI Mode keine offizielle API an.
    Das grounded Gemini ist KEIN Ersatz - es ist ein anderes Produkt mit eigenem
    Retrieval. AI Overview ist die Antwort auf der Suchergebnisseite und damit die
    fuer Sichtbarkeit relevanteste Oberflaeche.

    mode:
      "ai_overview" -> engine=google, liest data["ai_overview"]
                       (ggf. zweiter Call via page_token)
      "ai_mode"     -> engine=google_ai_mode
    """

    BASE = "https://serpapi.com/search"

    def __init__(self, api_key: str, mode: str = "ai_overview",
                 model: str = "google-ai-overview",
                 hl: str = "de", gl: str = "de", timeout: int = 90,
                 retries: int = 3):
        self.api_key = api_key
        self._retries = retries   # 17.07.2026, s. LLM-Clients
        self.mode = mode
        self.model = model
        self.hl = hl
        self.gl = gl
        self.timeout = timeout

    # --- Hilfsfunktionen ---------------------------------------------------

    def _flatten_blocks(self, blocks) -> str:
        """AI-Overview/AI-Mode text_blocks rekursiv zu Plaintext."""
        out = []
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            snip = b.get("snippet") or b.get("text") or ""
            if snip:
                out.append(snip)
            if btype == "list":
                for item in b.get("list") or []:
                    if isinstance(item, dict):
                        t = item.get("title") or ""
                        s = item.get("snippet") or ""
                        line = (t + ": " + s).strip(": ").strip()
                        if line:
                            out.append("- " + line)
            if btype == "table":
                for row in b.get("table") or []:
                    if isinstance(row, list):
                        out.append(" | ".join(str(c) for c in row))
            # verschachtelte Bloecke
            if b.get("text_blocks"):
                nested = self._flatten_blocks(b["text_blocks"])
                if nested:
                    out.append(nested)
        return "\n".join(x for x in out if x)

    def _refs_to_sources(self, refs):
        sources, seen = [], set()
        for r in refs or []:
            if not isinstance(r, dict):
                continue
            url = r.get("link") or r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append({
                "title": (r.get("title") or r.get("source") or "")[:200],
                "url": url,
            })
        return sources

    def _get(self, params):
        p = dict(params)
        p["api_key"] = self.api_key
        r = requests.get(self.BASE, params=p, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"SerpApi HTTP {r.status_code}: {r.text[:400]}")
        return r.json()

    # --- Hauptaufruf -------------------------------------------------------

    def ask(self, prompt: str) -> LLMResponse:
        def _call():
            t0 = time.time()

            if self.mode == "ai_mode":
                data = self._get({"engine": "google_ai_mode", "q": prompt,
                                  "hl": self.hl, "gl": self.gl})
                err = data.get("error")
                if err:
                    # "no results" ist ein legitimer Befund, kein Fehler
                    if "hasn't returned any results" in err or "not been found" in err:
                        return LLMResponse(text="", sources=[], model=self.model,
                                           latency_ms=(time.time() - t0) * 1000)
                    raise RuntimeError(f"SerpApi AI Mode: {err[:300]}")
                blocks = data.get("text_blocks") or []
                refs = data.get("references") or []
            else:
                data = self._get({"engine": "google", "q": prompt,
                                  "hl": self.hl, "gl": self.gl})
                err = data.get("error")
                if err and "hasn't returned any results" not in err:
                    raise RuntimeError(f"SerpApi google: {err[:300]}")
                ao = data.get("ai_overview") or {}
                blocks = ao.get("text_blocks") or []
                refs = ao.get("references") or []
                # Zweistufig: manche Antworten liefern nur ein page_token
                if not blocks and ao.get("page_token"):
                    data2 = self._get({"engine": "google_ai_overview",
                                       "page_token": ao["page_token"]})
                    err2 = data2.get("error")
                    if err2 and "hasn't returned any results" not in err2:
                        raise RuntimeError(f"SerpApi ai_overview: {err2[:300]}")
                    ao2 = data2.get("ai_overview") or {}
                    blocks = ao2.get("text_blocks") or []
                    refs = ao2.get("references") or []

            latency = (time.time() - t0) * 1000
            text = self._flatten_blocks(blocks)
            sources = self._refs_to_sources(refs)
            if not sources and text:
                sources = extract_urls_from_text(text)
            # Kein AI Overview / AI Mode ausgespielt = valides Ergebnis (Marke unsichtbar),
            # daher KEIN error, sondern leerer Text.
            if not text:
                print(f"[INFO] {self.model}: keine Antwort ausgespielt fuer Query "
                      f"'{prompt[:60]}...'")
            return LLMResponse(text=text, sources=sources, model=self.model,
                               latency_ms=latency)

        try:
            return with_retries(_call, attempts=self._retries)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(text="", sources=[], model=self.model,
                               latency_ms=0.0, error=str(e)[:500])


# ============================================================================
# Factory
# ============================================================================

def build_clients(llm_configs: List[Dict], settings: Optional[Dict] = None) -> Dict[str, object]:
    """
    Erzeugt die aktiven Clients basierend auf config.llms.
    API-Keys kommen aus Umgebungsvariablen:
        - ANTHROPIC_API_KEY  - Claude
        - GOOGLE_API_KEY     - Gemini
        - OPENAI_API_KEY     - ChatGPT (beide Kanaele: chatgpt und chatgpt_web)
        - SERPAPI_KEY        - Google AI Overview / AI Mode

    Zusaetzliche Schalter (04.08.2026), alle mit sicherem Default:
        - GEO_CHATGPT_WEB=0        schaltet die Websuche-Engine ab
        - GEO_RESOLVE_REDIRECTS=0  schaltet das Aufloesen der Gemini-
                                   Weiterleitungen ab (s. redirect_resolver.py)

    17.07.2026: `settings` (= config.json["settings"]) wird jetzt durchgereicht.
    Vorher bekamen die Clients NUR api_key und model; `temperature` und `max_tokens`
    blieben auf ihren Klassen-Defaults (0.3 / 1200). Dass das nie auffiel, lag daran,
    dass die Config exakt dieselben Werte nennt - wer sie dort aendert, haette aber
    keinerlei Wirkung gesehen. Eine Einstellung, die aussieht als wirke sie und nicht
    wirkt, ist schlimmer als gar keine.
    """
    st = settings or {}
    _temp = st.get("temperature")
    _maxtok = st.get("max_tokens")
    _kw = {}
    if isinstance(_temp, (int, float)) and not isinstance(_temp, bool):
        _kw["temperature"] = float(_temp)
    if isinstance(_maxtok, int) and not isinstance(_maxtok, bool) and _maxtok > 0:
        _kw["max_tokens"] = _maxtok
    # retry_attempts ist ab heute scharf - vorher war die Einstellung inert. Das Backoff
    # ist exponentiell (base_delay=2s, 2**i): 3 Versuche = 2+4 = 6 s Schlaf pro endgueltig
    # scheiterndem Call, 6 Versuche waeren schon 126 s. Bei ~650 Prompts und
    # timeout-minutes: 300 ist das ein Fuss-Schuss, den es vorher nicht geben KONNTE.
    # Deshalb gedeckelt.
    _ret = st.get("retry_attempts")
    if isinstance(_ret, int) and not isinstance(_ret, bool) and _ret > 0:
        if _ret > 5:
            print("[WARN] settings.retry_attempts=%d ist hoch - exponentielles Backoff "
                  "waechst auf 2+4+8+16+32... Sekunden je Call. Auf 5 gedeckelt." % _ret)
            _ret = 5
        _kw["retries"] = _ret

    clients: Dict[str, object] = {}
    for cfg in llm_configs:
        if not cfg.get("enabled"):
            continue
        provider = cfg["provider"]
        model = cfg["model"]
        if provider == "anthropic":
            key = os.getenv("ANTHROPIC_API_KEY")
            if not key:
                print("[WARN] ANTHROPIC_API_KEY fehlt — Claude wird übersprungen")
                continue
            clients[cfg["id"]] = ClaudeClient(api_key=key, model=model, **_kw)
        elif provider == "google":
            key = os.getenv("GOOGLE_API_KEY")
            if not key:
                print("[WARN] GOOGLE_API_KEY fehlt — Gemini wird übersprungen")
                continue
            clients[cfg["id"]] = GeminiClient(api_key=key, model=model, **_kw)
        elif provider == "openai":
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                print("[WARN] OPENAI_API_KEY fehlt - ChatGPT wird uebersprungen")
                continue
            clients[cfg["id"]] = OpenAIClient(api_key=key, model=model, **_kw)
        elif provider == "openai_web":
            # 04.08.2026: ChatGPT MIT Websuche, ZUSAETZLICH zu `chatgpt`.
            # Kostet je Lauf ein Vielfaches des textbasierten Kanals (Tool-Gebuehr
            # je Suchaufruf), deshalb zwei unabhaengige Schalter:
            #   1. config.json llms[].enabled  (im Cockpit klickbar)
            #   2. Umgebungsvariable GEO_CHATGPT_WEB=0 als Not-Aus, ohne Commit.
            if not _env_flag("GEO_CHATGPT_WEB", True):
                print(f"[COST] {cfg['id']} per GEO_CHATGPT_WEB=0 abgeschaltet - uebersprungen")
                continue
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                print(f"[WARN] OPENAI_API_KEY fehlt - {cfg['id']} wird uebersprungen")
                continue
            _web_kw = dict(_kw)
            clients[cfg["id"]] = OpenAIWebSearchClient(
                api_key=key, model=model,
                api=cfg.get("api", "auto"),
                search_context_size=cfg.get("search_context_size", "low"),
                **_web_kw,
            )
        elif provider == "xai":
            key = os.getenv("XAI_API_KEY")
            if not key:
                print("[WARN] XAI_API_KEY fehlt - Grok wird uebersprungen")
                continue
            clients[cfg["id"]] = GrokClient(api_key=key, model=model, **_kw)
        elif provider == "perplexity":
            key = os.getenv("PERPLEXITY_API_KEY")
            if not key:
                print("[WARN] PERPLEXITY_API_KEY fehlt - Perplexity wird uebersprungen")
                continue
            clients[cfg["id"]] = PerplexityClient(api_key=key, model=model, **_kw)
        elif provider in ("serpapi_ai_overview", "serpapi_ai_mode"):
            key = os.getenv("SERPAPI_KEY")
            if not key:
                print(f"[WARN] SERPAPI_KEY fehlt - {cfg['id']} wird uebersprungen")
                continue
            mode = "ai_overview" if provider == "serpapi_ai_overview" else "ai_mode"
            # kennt weder temperature noch max_tokens - nur retries durchreichen
            _serp_kw = {"retries": _kw["retries"]} if "retries" in _kw else {}
            clients[cfg["id"]] = SerpApiGoogleClient(api_key=key, mode=mode, model=model, **_serp_kw)
        else:
            print(f"[INFO] Provider {provider} noch nicht implementiert - skip")
    return clients
