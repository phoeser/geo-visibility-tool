"""
LLM-Clients für Gemini (Google) und Claude (Anthropic).

Beide Clients exponieren dieselbe Methode `ask(prompt)` und liefern ein
einheitliches Response-Schema:

{
    "text":     "<Antworttext>",
    "sources":  [ {"title": "...", "url": "..."}, ... ],   # falls verfügbar
    "model":    "<modell-id>",
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
            out.append({"title": "", "url": clean})
    return out


# --- Retry-Wrapper ----------------------------------------------------------

def with_retries(func, attempts: int = 3, base_delay: float = 2.0):
    """Exponentielles Backoff bei Fehlern."""
    last_err = None
    for i in range(attempts):
        try:
            return func()
        except Exception as e:  # noqa: BLE001
            last_err = e
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
            sources: List[Dict[str, str]] = []
            ground = candidates[0].get("groundingMetadata", {}) or {}
            for chunk in ground.get("groundingChunks", []) or []:
                web = chunk.get("web", {}) or {}
                if web.get("uri"):
                    sources.append({
                        "title": web.get("title", ""),
                        "url": web.get("uri", ""),
                    })
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
                    sources.append({"title": "", "url": c})
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
        - OPENAI_API_KEY     - ChatGPT
        - SERPAPI_KEY        - Google AI Overview / AI Mode

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
