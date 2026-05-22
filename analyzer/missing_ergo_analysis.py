"""
Missing-ERGO-Analyse:

Fuer jeden Prompt, bei dem ein LLM ERGO nicht erwaehnt hat, wird der jeweils
SELBE LLM mit einer Follow-up-Frage konfrontiert:
  "Du hast ERGO nicht erwaehnt. Warum nicht?"

Die freitextlichen Antworten werden anschliessend von einem Classifier-LLM
(ChatGPT bevorzugt, sonst Fallback) in feste Kategorien einsortiert. Aggregate
landen pro Produkt + LLM + Kategorie im Run-Dict unter Key "missing_ergo".

Wichtig:
- Es werden ausschliesslich die LLMs befragt, die im aktuellen Run aktiv
  waren (run["products"][pid]["per_llm"]).
- Gemini wird rate-limited (1 Call gleichzeitig, kleine Pause), damit das
  Modul den Tages-Quota nicht killt.
- Fehler einzelner Calls fuehren NICHT zum Abbruch - sie werden als
  "failed"-Eintrag mit Errormeldung gespeichert.

Input:  run_dict + clients-Dict {llm_id: client}
Output: Dict (wird unter run_dict["missing_ergo"] gespeichert)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import requests


# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

BRAND_NAME = "ERGO"  # Eigenmarke (kommt eigentlich aus run["brand"], hier safe-default)

CATEGORIES: List[str] = [
    "Preis/Beitrag",
    "Marktposition/Marktanteil",
    "Test-Sieger/Stiftung Warentest",
    "Marken-Awareness",
    "Leistungsumfang",
    "Online-Verfuegbarkeit/UX",
    "Zielgruppe/Positionierung",
    "Empfehlungs-Quellen",
    "Sonstiges",
]

MAX_RESPONSE_CHARS_IN_PROMPT = 600   # Wie viel von der Original-Antwort wir mitgeben
MAX_FOLLOWUP_RESPONSE = 1200          # LLM-Antwort wird auf so viele Chars gekappt
MAX_PROMPTS_PER_LLM_PER_PRODUCT = 50  # Sanity-Cap

GEMINI_PAUSE_SECONDS = 1.5            # Pause zwischen Gemini-Calls (Quota-Schutz)
OPENAI_PAUSE_SECONDS = 0.3
DEFAULT_PAUSE_SECONDS = 0.2


# ---------------------------------------------------------------------------
# Follow-up-Prompts (direkt-konfrontativ)
# ---------------------------------------------------------------------------

FOLLOWUP_SYSTEM = (
    "Du bist ein hilfreicher Assistent fuer Versicherungsfragen. "
    "Du gibst eine ehrliche und konkrete Selbst-Einschaetzung. "
    "Antworte auf Deutsch in 3-5 Saetzen ohne Markdown."
)

FOLLOWUP_USER_TMPL = """Bezug auf deine vorherige Antwort:

Urspruengliche Frage: {prompt}

Deine Antwort (Ausschnitt): "{response}"

In dieser Antwort hast du den Versicherer {brand} NICHT erwaehnt. Bitte erklaere
konkret und ehrlich, welche Gruende es gibt, warum {brand} nicht zu den von dir
genannten Anbietern gehoerte. Moegliche Aspekte sind z.B.: Preis/Beitrag,
Marktposition, Test-Sieger-Status, Marken-Awareness, Leistungsumfang,
Online-Verfuegbarkeit, Zielgruppe oder fehlende Empfehlungs-Quellen.

Nenne 1-3 konkrete Gruende in 3-5 Saetzen. Keine Floskeln, keine Entschuldigung."""


# ---------------------------------------------------------------------------
# Classifier (ChatGPT bevorzugt)
# ---------------------------------------------------------------------------

CLASSIFIER_SYSTEM = (
    "Du bist ein praeziser Text-Klassifizierer. Du klassifizierst kurze "
    "Begruendungen, warum eine Versicherungsmarke nicht erwaehnt wurde, in "
    "vorgegebene Kategorien. Antworte IMMER nur als JSON-Objekt."
)

CLASSIFIER_USER_TMPL = """Folgende Kategorien sind erlaubt:
- Preis/Beitrag
- Marktposition/Marktanteil
- Test-Sieger/Stiftung Warentest
- Marken-Awareness
- Leistungsumfang
- Online-Verfuegbarkeit/UX
- Zielgruppe/Positionierung
- Empfehlungs-Quellen
- Sonstiges

Begruendungs-Text:
\"\"\"{text}\"\"\"

Antworte mit EINEM JSON-Objekt im Schema:
{{
  "categories": ["..."],            // 1-3 Kategorien aus obiger Liste, Mehrfachzuordnung erlaubt
  "quote":      "..."               // 1 praegnanter Satz aus dem Text, max 200 Zeichen, woertlich
}}

Nur das JSON, kein Markdown."""


# ---------------------------------------------------------------------------
# Helfer
# ---------------------------------------------------------------------------

def _is_ergo_mentioned(result: Dict, brand: str = BRAND_NAME) -> bool:
    """True wenn die Marke in metrics.brands als mentioned=True geflaggt ist."""
    metrics = result.get("metrics") or {}
    brands = metrics.get("brands") or []
    for b in brands:
        if (b.get("name") or "").strip().lower() == brand.strip().lower():
            return bool(b.get("mentioned"))
    return False


def _snippet(text: str, limit: int = MAX_RESPONSE_CHARS_IN_PROMPT) -> str:
    if not text:
        return ""
    s = re.sub(r"\s+", " ", text).strip()
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _pause_for(llm_id: str) -> float:
    if llm_id == "gemini":
        return GEMINI_PAUSE_SECONDS
    if llm_id == "chatgpt":
        return OPENAI_PAUSE_SECONDS
    return DEFAULT_PAUSE_SECONDS


def _safe_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(s[start : end + 1])
        except Exception:
            pass
    # Regex-Fallback fuer truncated JSON
    out: Dict = {}
    m_cats = re.search(r'"categories"\s*:\s*\[([^\]]*)\]', s)
    if m_cats:
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', m_cats.group(1))
        out["categories"] = items
    m_q = re.search(r'"quote"\s*:\s*"((?:[^"\\]|\\.)*)"', s, re.DOTALL)
    if m_q:
        out["quote"] = m_q.group(1)
    return out or None


# ---------------------------------------------------------------------------
# Step 1: Follow-up-Calls an die LLMs, die ERGO nicht erwaehnt haben
# ---------------------------------------------------------------------------

def _collect_misses(run: Dict, brand: str = BRAND_NAME) -> Dict[str, List[Dict]]:
    """
    Liefert pro Produkt eine Liste von Treffern (Prompts, in denen ein LLM ERGO
    nicht erwaehnt hat). Jeder Treffer enthaelt:
      { "llm", "prompt_id", "prompt_text", "intent", "response_text" }
    """
    out: Dict[str, List[Dict]] = {}
    products = run.get("products") or {}
    for pid, prod in products.items():
        per_llm = prod.get("per_llm") or []
        misses: List[Dict] = []
        for llm_entry in per_llm:
            llm_id = llm_entry.get("llm")
            if not llm_id:
                continue
            results = llm_entry.get("results") or []
            count_for_llm = 0
            for r in results:
                if r.get("error"):
                    continue  # Initial-Call schon failed -> kein Follow-up
                if _is_ergo_mentioned(r, brand):
                    continue
                count_for_llm += 1
                if count_for_llm > MAX_PROMPTS_PER_LLM_PER_PRODUCT:
                    break
                misses.append({
                    "llm": llm_id,
                    "prompt_id": r.get("prompt_id"),
                    "prompt_text": r.get("prompt_text") or "",
                    "intent": r.get("intent") or "",
                    "response_text": r.get("response_text") or "",
                })
        out[pid] = misses
    return out


def _ask_followup(client, llm_id: str, prompt_text: str, response_text: str,
                  brand: str = BRAND_NAME) -> Tuple[Optional[str], Optional[str]]:
    """
    Schickt die Follow-up-Frage an den gegebenen Client.
    Return: (antwort_text, error_or_none)
    """
    user_msg = FOLLOWUP_USER_TMPL.format(
        prompt=_snippet(prompt_text, 400),
        response=_snippet(response_text, MAX_RESPONSE_CHARS_IN_PROMPT),
        brand=brand,
    )
    full_prompt = FOLLOWUP_SYSTEM + "\n\n" + user_msg
    try:
        resp = client.ask(full_prompt)
    except Exception as e:
        return None, f"call exception: {str(e)[:200]}"
    err = getattr(resp, "error", None)
    if err:
        return None, f"llm error: {str(err)[:200]}"
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        return None, "empty response"
    return text[:MAX_FOLLOWUP_RESPONSE], None


# ---------------------------------------------------------------------------
# Step 2: Klassifikation der Antworten in feste Kategorien
# ---------------------------------------------------------------------------

def _classify_with_openai(text: str, model: str = "gpt-4o-mini",
                          timeout: int = 30) -> Optional[Dict]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0.0,
                "max_tokens": 250,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": CLASSIFIER_SYSTEM},
                    {"role": "user", "content": CLASSIFIER_USER_TMPL.format(text=text[:1500])},
                ],
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        return _safe_json(content or "")
    except Exception as e:
        print(f"[MISSING-ERGO] Classifier-Fehler: {e}")
        return None


def _classify_heuristic(text: str) -> Dict:
    """
    Fallback ohne LLM: Stichwoerter-Matching auf die festen Kategorien.
    """
    t = (text or "").lower()
    hits: List[str] = []

    def _has(*kws):
        return any(k in t for k in kws)

    if _has("preis", "beitrag", "guenstig", "günstig", "tarif", "kosten"):
        hits.append("Preis/Beitrag")
    if _has("marktanteil", "marktposition", "marktfuehrer", "marktführer", "groesse", "größe", "marktdurchdringung"):
        hits.append("Marktposition/Marktanteil")
    if _has("stiftung warentest", "testsieger", "test-sieger", "testurteil", "oekotest", "öko test", "finanztest"):
        hits.append("Test-Sieger/Stiftung Warentest")
    if _has("bekanntheit", "marken", "awareness", "marketing", "image", "image-"):
        hits.append("Marken-Awareness")
    if _has("leistung", "tarifumfang", "feature", "extras", "deckung"):
        hits.append("Leistungsumfang")
    if _has("online", "digital", "app", "website", "abschluss"):
        hits.append("Online-Verfuegbarkeit/UX")
    if _has("zielgruppe", "segment", "positionierung", "fokus"):
        hits.append("Zielgruppe/Positionierung")
    if _has("vergleich.de", "check24", "verivox", "finanztip", "stiftung", "trustpilot", "ekomi", "empfehlung"):
        hits.append("Empfehlungs-Quellen")
    if not hits:
        hits.append("Sonstiges")
    # Kurzes Quote: ersten satz extrahieren
    quote = re.split(r"(?<=[\.!?])\s+", text.strip(), maxsplit=1)[0]
    return {"categories": hits[:3], "quote": quote[:200]}


def _classify(text: str) -> Dict:
    """Bevorzugt OpenAI, fallback heuristisch."""
    parsed = _classify_with_openai(text)
    if parsed and parsed.get("categories"):
        cats = []
        for c in parsed.get("categories", []):
            c = str(c).strip()
            # auf vorgegebene Kategorien normalisieren (case-insensitive Match)
            match = next((cat for cat in CATEGORIES if cat.lower() == c.lower()), None)
            if match:
                cats.append(match)
            elif c:
                # zumindest in "Sonstiges" packen, wenn LLM was eigenes erfindet
                if "Sonstiges" not in cats:
                    cats.append("Sonstiges")
        if not cats:
            cats = ["Sonstiges"]
        quote = str(parsed.get("quote") or "").strip()[:200]
        return {"categories": cats[:3], "quote": quote}
    return _classify_heuristic(text)


# ---------------------------------------------------------------------------
# Step 3: Aggregation
# ---------------------------------------------------------------------------

def _aggregate(prod_id: str, prod_label: str, misses: List[Dict],
               followups: List[Dict], active_llms: List[str]) -> Dict:
    """
    misses + followups sind 1:1 verknuepft (gleicher Index oder via prompt_id+llm).
    Aggregiert: top-Kategorien, by_llm, drilldown.
    """
    # Drilldown gruppieren nach prompt_id
    by_prompt: Dict[str, Dict] = {}
    cat_counter: Dict[str, int] = {c: 0 for c in CATEGORIES}
    cat_by_llm: Dict[str, Dict[str, int]] = {c: {} for c in CATEGORIES}
    followups_per_llm: Dict[str, Dict[str, int]] = {
        llm: {"count": 0, "successful": 0, "failed": 0} for llm in active_llms
    }
    examples_for_cat: Dict[str, List[Dict]] = {c: [] for c in CATEGORIES}

    for fu in followups:
        llm = fu["llm"]
        pid_local = fu["prompt_id"] or "?"
        prompt_text = fu["prompt_text"]
        intent = fu.get("intent") or ""
        success = fu.get("success", False)
        answer = fu.get("answer") or ""
        categories = fu.get("categories") or []
        quote = fu.get("quote") or ""
        error = fu.get("error")

        followups_per_llm.setdefault(llm, {"count": 0, "successful": 0, "failed": 0})
        followups_per_llm[llm]["count"] += 1
        if success:
            followups_per_llm[llm]["successful"] += 1
        else:
            followups_per_llm[llm]["failed"] += 1

        # Kategorien-Counter
        for c in categories:
            if c not in cat_counter:
                cat_counter[c] = 0
                cat_by_llm[c] = {}
                examples_for_cat[c] = []
            cat_counter[c] += 1
            cat_by_llm[c][llm] = cat_by_llm[c].get(llm, 0) + 1
            if quote and len(examples_for_cat[c]) < 4:
                examples_for_cat[c].append({"llm": llm, "quote": quote, "prompt_id": pid_local})

        # Drilldown
        if pid_local not in by_prompt:
            by_prompt[pid_local] = {
                "prompt_id": pid_local,
                "prompt_text": prompt_text,
                "intent": intent,
                "missing_in_llms": [],
                "responses": [],
            }
        by_prompt[pid_local]["missing_in_llms"].append(llm)
        by_prompt[pid_local]["responses"].append({
            "llm": llm,
            "success": success,
            "categories": categories,
            "quote": quote,
            "answer": answer,
            "error": error,
        })

    # Top-Kategorien (sortiert nach count desc, nur >0)
    total_classified = sum(1 for fu in followups if fu.get("categories"))
    cat_list = []
    for cat, count in sorted(cat_counter.items(), key=lambda kv: -kv[1]):
        if count <= 0:
            continue
        cat_list.append({
            "category": cat,
            "count": count,
            "share": (count / total_classified) if total_classified > 0 else 0.0,
            "by_llm": cat_by_llm.get(cat, {}),
            "examples": examples_for_cat.get(cat, []),
        })

    # Anzahl distinct Prompts ohne ERGO
    distinct_prompts = len(by_prompt)

    return {
        "product_label": prod_label,
        "prompts_without_ergo": distinct_prompts,
        "followups_total": len(followups),
        "followups_per_llm": followups_per_llm,
        "categories_top": cat_list,
        "drilldown": list(by_prompt.values()),
    }


# ---------------------------------------------------------------------------
# Haupt-Entry: analyze_run()
# ---------------------------------------------------------------------------

def analyze_run(run: Dict, clients: Dict, brand: Optional[str] = None,
                max_total_followups: int = 250) -> Dict:
    """
    Args:
      run         : aktuelles run_dict nach Haupt-LLM-Lauf
      clients     : Dict[llm_id -> client] (von build_clients() aus llm_clients.py)
      brand       : Eigenmarke (Default aus run["brand"] oder "ERGO")
      max_total_followups: Sicherheits-Cap fuer den Gesamtlauf

    Return: Dict, geeignet als run_dict["missing_ergo"].
    """
    brand = brand or (run.get("brand") or BRAND_NAME)
    products = run.get("products") or {}

    # 1) Misses sammeln (LLMs sind automatisch nur die im Lauf aktiven)
    misses_by_product = _collect_misses(run, brand=brand)

    # Liste der aktiven LLMs ableiten (aus per_llm-Listen, unique)
    active_llms: List[str] = []
    for prod in products.values():
        for entry in prod.get("per_llm") or []:
            lid = entry.get("llm")
            if lid and lid not in active_llms:
                active_llms.append(lid)

    print(f"[MISSING-ERGO] Aktive LLMs im Run: {active_llms}")
    total_misses = sum(len(v) for v in misses_by_product.values())
    print(f"[MISSING-ERGO] Prompts ohne {brand}: {total_misses} (cap: {max_total_followups})")

    if total_misses == 0:
        return {
            "_meta": {
                "brand": brand,
                "active_llms": active_llms,
                "categories": CATEGORIES,
                "followups_total": 0,
                "successful": 0,
                "failed": 0,
                "skipped_due_to_cap": 0,
                "classifier": "openai-gpt-4o-mini" if os.getenv("OPENAI_API_KEY") else "heuristic",
            },
            "by_product": {pid: _aggregate(pid, (products.get(pid) or {}).get("name") or pid, [], [], active_llms)
                           for pid in products.keys()},
        }

    # 2) Follow-ups ausfuehren, mit Quota-Schutz
    followups_by_product: Dict[str, List[Dict]] = {}
    overall_done = 0
    overall_success = 0
    overall_fail = 0
    skipped = 0

    for pid, misses in misses_by_product.items():
        followups_by_product[pid] = []
        for miss in misses:
            if overall_done >= max_total_followups:
                skipped += 1
                continue
            llm = miss["llm"]
            client = clients.get(llm)
            fu_entry: Dict = {
                **miss,
                "success": False,
                "answer": "",
                "error": None,
                "categories": [],
                "quote": "",
            }
            if not client:
                fu_entry["error"] = f"no client for llm '{llm}'"
                followups_by_product[pid].append(fu_entry)
                overall_fail += 1
                overall_done += 1
                continue

            answer, err = _ask_followup(client, llm, miss["prompt_text"],
                                        miss["response_text"], brand=brand)
            if err or not answer:
                fu_entry["error"] = err or "no answer"
                followups_by_product[pid].append(fu_entry)
                overall_fail += 1
                overall_done += 1
                time.sleep(_pause_for(llm))
                continue

            fu_entry["success"] = True
            fu_entry["answer"] = answer

            # 3) Klassifikation
            cls = _classify(answer)
            fu_entry["categories"] = cls.get("categories") or ["Sonstiges"]
            fu_entry["quote"] = cls.get("quote") or _snippet(answer, 200)

            followups_by_product[pid].append(fu_entry)
            overall_success += 1
            overall_done += 1
            time.sleep(_pause_for(llm))

            if overall_done % 10 == 0:
                print(f"[MISSING-ERGO] Progress: {overall_done}/{total_misses} done "
                      f"(success={overall_success}, fail={overall_fail})")

    # 4) Aggregation pro Produkt
    by_product: Dict[str, Dict] = {}
    for pid in products.keys():
        prod_label = (products.get(pid) or {}).get("name") or pid
        by_product[pid] = _aggregate(
            pid, prod_label,
            misses_by_product.get(pid, []),
            followups_by_product.get(pid, []),
            active_llms,
        )

    print(f"[MISSING-ERGO] fertig: done={overall_done}, success={overall_success}, "
          f"fail={overall_fail}, skipped={skipped}")

    return {
        "_meta": {
            "brand": brand,
            "active_llms": active_llms,
            "categories": CATEGORIES,
            "followups_total": overall_done,
            "successful": overall_success,
            "failed": overall_fail,
            "skipped_due_to_cap": skipped,
            "classifier": "openai-gpt-4o-mini" if os.getenv("OPENAI_API_KEY") else "heuristic",
        },
        "by_product": by_product,
    }
