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
                 max_tokens: int = 1200, temperature: float = 0.3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
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
            return with_retries(_call, attempts=3)
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
                 max_tokens: int = 1200, temperature: float = 0.3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
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
            return with_retries(_call, attempts=3)
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
                 max_tokens: int = 1200, temperature: float = 0.3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
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
            return with_retries(_call, attempts=3)
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
                 max_tokens: int = 1200, temperature: float = 0.3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
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
            return with_retries(_call, attempts=3)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(text="", sources=[], model=self.model,
                               latency_ms=0.0, error=str(e)[:500])


# ============================================================================
# Perplexity (Sonar)  -  OpenAI-kompatibel mit eingebauter Web-Suche
# ============================================================================

class PerplexityClient:
    """Ruft Perplexity Sonar ueber Chat-Completions auf. Web-Suche integriert."""

    def __init__(self, api_key: str, model: str = "sonar",
                 max_tokens: int = 1200, temperature: float = 0.3):
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
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
            return with_retries(_call, attempts=3)
        except Exception as e:  # noqa: BLE001
            return LLMResponse(text="", sources=[], model=self.model,
                               latency_ms=0.0, error=str(e)[:500])

# ============================================================================
# Factory
# ============================================================================

def build_clients(llm_configs: List[Dict]) -> Dict[str, object]:
    """
    Erzeugt die aktiven Clients basierend auf config.llms.
    API-Keys kommen aus Umgebungsvariablen:
        - ANTHROPIC_API_KEY  - Claude
        - GOOGLE_API_KEY     - Gemini
        - OPENAI_API_KEY     - ChatGPT
    """
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
            clients[cfg["id"]] = ClaudeClient(api_key=key, model=model)
        elif provider == "google":
            key = os.getenv("GOOGLE_API_KEY")
            if not key:
                print("[WARN] GOOGLE_API_KEY fehlt — Gemini wird übersprungen")
                continue
            clients[cfg["id"]] = GeminiClient(api_key=key, model=model)
        elif provider == "openai":
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                print("[WARN] OPENAI_API_KEY fehlt - ChatGPT wird uebersprungen")
                continue
            clients[cfg["id"]] = OpenAIClient(api_key=key, model=model)
        elif provider == "xai":
            key = os.getenv("XAI_API_KEY")
            if not key:
                print("[WARN] XAI_API_KEY fehlt - Grok wird uebersprungen")
                continue
            clients[cfg["id"]] = GrokClient(api_key=key, model=model)
        elif provider == "perplexity":
            key = os.getenv("PERPLEXITY_API_KEY")
            if not key:
                print("[WARN] PERPLEXITY_API_KEY fehlt - Perplexity wird uebersprungen")
                continue
            clients[cfg["id"]] = PerplexityClient(api_key=key, model=model)
        else:
            print(f"[INFO] Provider {provider} noch nicht implementiert - skip")
    return clients
