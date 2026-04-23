"""
LLM-basierte Klassifikation von Webseiten-Änderungen.

Für jedes Change-Event wird Gemini gefragt, *was* sich fachlich verändert
hat (Preis/Leistung/FAQ/Struktur/Copy), *wie* (hinzugefügt, entfernt,
umformuliert) und ob die Veränderung für Kunden spürbar ist.

Das Ergebnis ist ein kleines JSON, das an das Event angehängt wird:

    {
      "type": "preis" | "leistung" | "faq" | "copy" | "struktur" | "sonstiges",
      "direction": "add" | "remove" | "change",
      "magnitude": "minor" | "medium" | "major",
      "summary": "Kurze Beschreibung (max 200 Zeichen)",
      "keywords": ["stichwort1", "stichwort2"]
    }

Design-Prinzipien:
 - Wenn kein GOOGLE_API_KEY gesetzt ist oder die API fehlschlägt, gibt der
   Classifier `None` zurück — der Event-Eintrag ist weiterhin gültig.
 - Ein einzelner Call pro Event, kompakter Prompt, `responseMimeType: application/json`
   damit wir direkt ein sauberes JSON zurückbekommen.
 - Snippets werden begrenzt, damit wir das Kontextfenster nicht sprengen.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Dict, List, Optional

import requests


GEMINI_MODEL = "gemini-2.5-flash"
ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)

SYSTEM = (
    "Du bist Analyst für Versicherungs-Content. Du bekommst die Zeilen, die "
    "sich auf einer Produkt-Webseite geändert haben, und klassifizierst die "
    "Änderung. Antworte NUR mit einem JSON-Objekt gemäß Schema, ohne Prosa."
)

PROMPT_TMPL = """Webseite: {url}
Zusammenfassung der Änderung: {summary}

Neue Zeilen (+):
{added}

Entfernte Zeilen (-):
{removed}

Klassifiziere dies fachlich für ein Versicherungs-Produkt.

Schema:
{{
  "type": "preis" | "leistung" | "faq" | "copy" | "struktur" | "sonstiges",
  "direction": "add" | "remove" | "change",
  "magnitude": "minor" | "medium" | "major",
  "summary": "maximal 200 Zeichen auf Deutsch",
  "keywords": ["<lowercase, max 5>"]
}}

Leitlinien:
- "preis": Preise, Beiträge, Rabatte, Staffeln
- "leistung": Leistungsumfang, Erstattungssätze, Einschränkungen, Limits
- "faq": Frage-Antwort-Inhalte, Erklärungen für Kunden
- "copy": Marketing-Text, Claims, Überschriften ohne Substanz
- "struktur": Navigation, Buttons, Layout, Tabellen-Gerüst
- "magnitude": "minor" = kosmetisch, "medium" = spürbar, "major" = wesentliche Produktaussage
"""


def _truncate(lines: List[str], max_chars: int = 3000) -> str:
    acc = []
    size = 0
    for ln in lines:
        if size + len(ln) + 1 > max_chars:
            acc.append("…")
            break
        acc.append(ln)
        size += len(ln) + 1
    return "\n".join(acc) if acc else "—"


def _api_key() -> Optional[str]:
    key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    return key or None


def _safe_json_parse(text: str) -> Optional[dict]:
    if not text:
        return None
    # Remove possible code-fences
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t, flags=re.MULTILINE).strip()
    t = re.sub(r"```\s*$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except Exception:
        # letzte Chance: suche das erste {…}-Objekt
        m = re.search(r"\{.*\}", t, flags=re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def classify_diff(
    url: str,
    added_lines: List[str],
    removed_lines: List[str],
    summary: str,
    *,
    api_key: Optional[str] = None,
    model: str = GEMINI_MODEL,
    timeout: int = 45,
) -> Optional[Dict]:
    key = api_key or _api_key()
    if not key:
        return None
    if not added_lines and not removed_lines:
        return None

    prompt = PROMPT_TMPL.format(
        url=url,
        summary=summary[:300],
        added=_truncate(added_lines, 3000),
        removed=_truncate(removed_lines, 3000),
    )

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 400,
            "responseMimeType": "application/json",
        },
    }
    try:
        url_ = ENDPOINT.format(model=model, key=key)
        r = requests.post(url_, json=payload, timeout=timeout, headers={"content-type": "application/json"})
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        data = r.json()
        parts = (((data.get("candidates") or [{}])[0]).get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        parsed = _safe_json_parse(text)
        if not parsed:
            return {"error": "invalid json", "raw": text[:200]}

        # Normalisieren / Pflichtfelder absichern
        parsed["type"] = parsed.get("type") or "sonstiges"
        parsed["direction"] = parsed.get("direction") or "change"
        parsed["magnitude"] = parsed.get("magnitude") or "minor"
        parsed["summary"] = (parsed.get("summary") or "").strip()[:240]
        kw = parsed.get("keywords") or []
        if isinstance(kw, str):
            kw = [kw]
        parsed["keywords"] = [str(x).strip().lower()[:40] for x in kw[:5] if str(x).strip()]
        return parsed
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


def make_classifier(api_key: Optional[str] = None, model: str = GEMINI_MODEL) -> Callable:
    """
    Factory: gibt eine Closure zurück, die in page_tracker.track_page als
    `classifier=...` gereicht werden kann.
    """
    key = api_key or _api_key()

    def _inner(url: str, added: List[str], removed: List[str], summary: str) -> Optional[Dict]:
        if not key:
            return None
        return classify_diff(url, added, removed, summary, api_key=key, model=model)

    return _inner
