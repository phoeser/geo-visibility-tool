"""
Warum-Analyse: pro (Produkt, Marke) eine strukturierte Claude-Analyse,
warum die Marke in LLM-Antworten genannt bzw. nicht genannt wird.

Input:   run_dict nach Haupt-Lauf + claude_client (ClaudeClient)
Output:  Dict[product_id, Dict[brand_name, AnalysisDict]]
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

import requests

MAX_POSITIVE_EXAMPLES = 8
MAX_NEGATIVE_EXAMPLES = 8
MAX_SNIPPET_CHARS = 240

SYSTEM_PROMPT = (
    "Du bist ein erfahrener SEO- und LLM-Visibility-Analyst. "
    "Deine Aufgabe: Analysiere, warum eine bestimmte Versicherungsmarke in "
    "KI-generierten Empfehlungen genannt oder nicht genannt wird. "
    "Antworte immer auf Deutsch und streng als JSON-Objekt ohne Markdown-Wrapping."
)

USER_TMPL = """Produkt: {product_label}
Zu analysierende Marke: {brand}
Share of Voice dieser Marke im Lauf: {sov_pct:.1f} %
Top-5 Wettbewerber mit Share of Voice:
{competitors_block}

=== {n_pos} Beispiele wo {brand} GENANNT wird ===
{positive_block}

=== {n_neg} Beispiele wo {brand} NICHT genannt wird (aber andere schon) ===
{negative_block}

Analysiere die Muster. Liefere EIN JSON-Objekt mit genau folgenden Schluesseln:

{{
  "reasons_mentioned":      "2-3 Saetze: wann/warum wird {brand} erwaehnt?",
  "reasons_absent":         "2-3 Saetze: warum fehlt {brand} in anderen Antworten?",
  "key_topics":             ["max 5 Themen / Features wo {brand} stark ist"],
  "missing_topics":         ["max 5 Themen wo {brand} fehlt aber gefragt ist"],
  "example_quote_positive": "1 kurzer Original-Satz (max 200 Zeichen) aus den GENANNT-Beispielen",
  "example_quote_negative": "1 kurzer Original-Satz aus den NICHT-genannt-Beispielen, wo andere Marken stehen",
  "improvement_suggestions":["max 3 konkrete SEO/Content-Massnahmen, die {brand}s Sichtbarkeit erhoehen wuerden"]
}}

NICHT erfinden. Wenn die Daten nichts hergeben, Feld leer lassen ("" oder []).
Kein Markdown, kein Fliesstext um das JSON - nur das JSON selbst."""


def _snippet(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    if not text:
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    if len(s) > limit:
        s = s[: limit - 1] + "\u2026"
    return s


def _safe_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    s = text.strip()
    # Codefences entfernen
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    # Direkt parsen
    try:
        return json.loads(s)
    except Exception:
        pass
    # Erste vollstaendige JSON-Klammer rauspicken (mit Balance-Counter, damit
    # verschachtelte Objekte/Kommentare den rfind nicht stoeren)
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        # Letzter Fallback: greedy bis letzte Klammer
        end = s.rfind("}")
        if end <= start:
            return None
    try:
        return json.loads(s[start : end + 1])
    except Exception:
        pass
    # Notfall-Fallback: einzelne Felder per Regex retten (truncated/malformed JSON)
    return _regex_extract_fields(s)


def _regex_extract_fields(s: str) -> Optional[Dict]:
    """
    Wenn JSON-Parse scheitert (Truncation, Encoding-Probleme), extrahiere die
    wichtigsten Felder per Regex, damit der Why-Tab trotzdem Inhalte anzeigt.
    """
    if not s:
        return None
    out: Dict = {}
    # String-Felder: "key": "value"   (auch mit escapten Quotes)
    for field in ("reasons_mentioned", "reasons_absent",
                  "example_quote_positive", "example_quote_negative"):
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', s, re.DOTALL)
        if m:
            try:
                out[field] = json.loads(f'"{m.group(1)}"')
            except Exception:
                out[field] = m.group(1)
    # Listen-Felder: "key": [ ... ]
    for field in ("key_topics", "missing_topics", "improvement_suggestions"):
        m = re.search(rf'"{field}"\s*:\s*\[([^\]]*)\]', s, re.DOTALL)
        if m:
            items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
            out[field] = []
            for it in items:
                try:
                    out[field].append(json.loads(f'"{it}"'))
                except Exception:
                    out[field].append(it)
    return out if out else None


def _gather_examples(run: Dict, product_id: str, target_brand: str) -> Dict:
    product = (run.get("products") or {}).get(product_id) or {}
    per_llm = product.get("per_llm") or []
    competitors_by_brand: Dict[str, int] = {}
    positives: List[Dict] = []
    negatives: List[Dict] = []

    for llm_entry in per_llm:
        llm = llm_entry.get("llm") or "?"
        results = llm_entry.get("results") or []
        for r in results:
            metrics = r.get("metrics") or {}
            brands_list = metrics.get("brands") or []
            mentioned_names = [b.get("name") for b in brands_list if b.get("mentioned")]
            for b in brands_list:
                if b.get("mentioned") and b.get("name") and b.get("name") != target_brand:
                    competitors_by_brand[b["name"]] = competitors_by_brand.get(b["name"], 0) + 1
            is_target_mentioned = target_brand in mentioned_names
            others = [n for n in mentioned_names if n != target_brand]
            snippet = _snippet(r.get("response_text") or "")
            prompt_text = _snippet(r.get("prompt_text") or "", limit=120)
            entry = {
                "prompt_id": r.get("prompt_id"),
                "prompt_intent": r.get("intent") or "",
                "prompt_text": prompt_text,
                "llm": llm,
                "mentioned": mentioned_names,
                "others": others[:5],
                "snippet": snippet,
            }
            if is_target_mentioned and snippet:
                positives.append(entry)
            elif not is_target_mentioned and others and snippet:
                negatives.append(entry)

    def _sample(items: List[Dict], max_n: int) -> List[Dict]:
        if len(items) <= max_n:
            return items
        by_llm: Dict[str, List[Dict]] = {}
        for it in items:
            by_llm.setdefault(it["llm"], []).append(it)
        out: List[Dict] = []
        while len(out) < max_n and any(by_llm.values()):
            for llm in list(by_llm.keys()):
                if by_llm[llm]:
                    out.append(by_llm[llm].pop(0))
                    if len(out) >= max_n:
                        break
        return out

    return {
        "positives": _sample(positives, MAX_POSITIVE_EXAMPLES),
        "negatives": _sample(negatives, MAX_NEGATIVE_EXAMPLES),
        "competitors_by_brand": competitors_by_brand,
        "total_positives": len(positives),
        "total_negatives": len(negatives),
    }


def _brand_sov_from_summary(run: Dict, product_id: str, brand: str) -> float:
    product = (run.get("products") or {}).get(product_id) or {}
    sbl = product.get("summary_by_llm") or {}
    vals: List[float] = []
    for _, s in sbl.items():
        row = next((b for b in (s.get("brands") or []) if b.get("name") == brand), None)
        if row and row.get("share_of_voice") is not None:
            vals.append(float(row["share_of_voice"]))
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _competitor_block(counts: Dict[str, int], top_n: int = 5) -> str:
    if not counts:
        return "(keine Daten)"
    total = sum(counts.values()) or 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:top_n]
    lines = []
    for name, n in top:
        pct = 100.0 * n / total
        lines.append(f"  - {name}: {pct:.1f}% Nennungsanteil")
    return "\n".join(lines)


def _format_example(e: Dict) -> str:
    intent = f"[{e.get('prompt_intent','')}]" if e.get("prompt_intent") else ""
    llm = f"({e.get('llm','?')})"
    prompt = e.get("prompt_text", "")
    others = e.get("others") or []
    snippet = e.get("snippet", "")
    extra = f" | andere genannt: {', '.join(others)}" if others else ""
    return f"- {llm}{intent} Prompt: {prompt}\n  Antwort-Snippet: \"{snippet}\"{extra}"




GEMINI_JSON_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


def _call_gemini_json(prompt: str, system_prompt: str, model: str = "gemini-2.5-flash",
                     max_tokens: int = 4000, timeout: int = 60,
                     json_mode: bool = True) -> Optional[str]:
    """
    Direkt-Aufruf an Gemini. Mit json_mode=True wird responseMimeType=application/json
    gesetzt (strikter aber ggf. early-truncation), ohne kommt freier Text zurueck den
    _safe_json regex-extrahiert.
    """
    api_key = os.getenv("GOOGLE_API_KEY") or ""
    if not api_key:
        return None
    gen_config = {
        "temperature": 0.2,
        "maxOutputTokens": max_tokens,
    }
    if json_mode:
        gen_config["responseMimeType"] = "application/json"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_config,
    }
    try:
        url = GEMINI_JSON_ENDPOINT.format(model=model, key=api_key)
        r = requests.post(url, json=payload, timeout=timeout,
                          headers={"content-type": "application/json"})
        if r.status_code != 200:
            return None
        data = r.json()
        parts = (((data.get("candidates") or [{}])[0]).get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts) or None
    except Exception as e:
        print(f"[WHY] Gemini-Direct-Call Fehler: {e}")
        return None



def analyze_brand_for_product(run: Dict, product_id: str, brand: str, claude_client) -> Optional[Dict]:
    product = (run.get("products") or {}).get(product_id) or {}
    product_label = product.get("name") or product_id
    data = _gather_examples(run, product_id, brand)
    sov = _brand_sov_from_summary(run, product_id, brand)
    positives = data["positives"]
    negatives = data["negatives"]
    if not positives and not negatives:
        return {
            "reasons_mentioned": "",
            "reasons_absent": "Marke taucht weder positiv noch negativ in den Antworten auf.",
            "key_topics": [],
            "missing_topics": [],
            "example_quote_positive": "",
            "example_quote_negative": "",
            "improvement_suggestions": [],
            "_meta": {"total_positives": 0, "total_negatives": 0, "skipped": True, "sov": sov},
        }
    prompt = USER_TMPL.format(
        product_label=product_label,
        brand=brand,
        sov_pct=sov * 100.0,
        competitors_block=_competitor_block(data["competitors_by_brand"]),
        n_pos=data["total_positives"],
        n_neg=data["total_negatives"],
        positive_block="\n".join(_format_example(e) for e in positives) or "(keine Beispiele)",
        negative_block="\n".join(_format_example(e) for e in negatives) or "(keine Beispiele)",
    )
    # Bei Gemini: 2-stufiger Direct-Call (1. mit JSON-Mode, 2. ohne als Fallback)
    client_class = type(claude_client).__name__
    if client_class == "GeminiClient":
        model = getattr(claude_client, "model", "gemini-2.5-flash")
        # Versuch 1: JSON-Mode + 4000 Tokens
        text = _call_gemini_json(prompt, SYSTEM_PROMPT, model=model, max_tokens=4000, json_mode=True)
        parsed = _safe_json(text) if text else None
        if not parsed:
            # Versuch 2: ohne responseMimeType (Fallback bei Gemini-Truncation in JSON-Mode)
            print(f"[WHY] JSON-Mode fail fuer {brand}/{product_id}, versuche ohne responseMimeType")
            text2 = _call_gemini_json(prompt, SYSTEM_PROMPT, model=model, max_tokens=4000, json_mode=False)
            parsed = _safe_json(text2) if text2 else None
            if not parsed:
                return {"error": "Gemini-JSON nicht parsebar (beide Versuche)",
                        "raw_v1": (text or "")[:1500],
                        "raw_v2": (text2 or "")[:1500]}
    else:
        # Claude / OpenAI: System-Prompt vorne dranhaengen, normaler Client-Call
        full_prompt = SYSTEM_PROMPT + "\n\n" + prompt
        try:
            resp = claude_client.ask(full_prompt)
        except Exception as e:
            return {"error": f"Call fehlgeschlagen: {str(e)[:200]}"}
        if getattr(resp, "error", None):
            return {"error": f"LLM-Error: {resp.error[:200]}"}
        parsed = _safe_json(getattr(resp, "text", "") or "")
        if not parsed:
            return {"error": "LLM-Antwort nicht als JSON parsebar", "raw": (getattr(resp, "text", "") or "")[:2000]}
    return {
        "reasons_mentioned": str(parsed.get("reasons_mentioned") or "")[:500],
        "reasons_absent": str(parsed.get("reasons_absent") or "")[:500],
        "key_topics": [str(x)[:60] for x in (parsed.get("key_topics") or [])][:5],
        "missing_topics": [str(x)[:60] for x in (parsed.get("missing_topics") or [])][:5],
        "example_quote_positive": str(parsed.get("example_quote_positive") or "")[:260],
        "example_quote_negative": str(parsed.get("example_quote_negative") or "")[:260],
        "improvement_suggestions": [str(x)[:200] for x in (parsed.get("improvement_suggestions") or [])][:3],
        "_meta": {"total_positives": data["total_positives"], "total_negatives": data["total_negatives"], "sov": sov},
    }


def analyze_run(run: Dict, claude_client, brands: Optional[List[str]] = None) -> Dict[str, Dict[str, Dict]]:
    if brands is None:
        own = run.get("brand") or ""
        comp_list = [c for c in (run.get("competitors") or []) if isinstance(c, str)]
        # Vorher: comp_list[:3] -> HUK-Coburg fiel raus.
        # Jetzt: alle Wettbewerber analysieren.
        brands = [b for b in [own] + comp_list if b]
    out: Dict[str, Dict[str, Dict]] = {}
    products = run.get("products") or {}
    for pid in products.keys():
        out[pid] = {}
        for b in brands:
            print(f"[WHY] {pid} / {b} ...")
            out[pid][b] = analyze_brand_for_product(run, pid, b, claude_client) or {}
    return out
