#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kontrolliertes A/B-Experiment: Was macht die Websuche mit der ERGO-Sichtbarkeit?

WARUM DIESES SKRIPT UEBERHAUPT EXISTIERT
----------------------------------------
Im Lauf vom 05.08.2026 hat der Kanal `chatgpt_web` (Responses-API, gpt-4.1-mini,
tools:[{"type":"web_search"}], Default tool_choice: auto) bei 226 von 323 Prompts
(70 %) SELBST entschieden zu suchen. Gemessen wurde:

    mit Websuche  (226 Antworten):  SoV 10,54 %   genannt in 27,9 %
    ohne Websuche ( 97 Antworten):  SoV  3,16 %   genannt in 14,4 %

Diese Gegenueberstellung ist KONFUNDIERT und darf so nicht als Wirkung der
Websuche gelesen werden. Das Modell entscheidet selbst, wann es sucht — die
beiden Gruppen enthalten also unterschiedliche FRAGEN, nicht dieselben Fragen
unter zwei Bedingungen. Dass die Suchquote stark nach Produkt schwankt
(Reise 87 %, Risikoleben 43 %), zeigt genau das: gesucht wird bei den
aktualitaets- und anbieterlastigen Fragen, und das sind zufaellig auch die,
bei denen ERGO ohnehin haeufiger auftaucht. Die Selektion erklaert einen
unbekannten Teil des Unterschieds — vielleicht den ganzen.

Die saubere Version ist ein gepaartes Experiment: DIESELBEN Prompts, zweimal,
zum selben Zeitpunkt, mit demselben Modell und demselben System-Prompt:

    Arm A  tool_choice="required"   Suche erzwungen
    Arm B  ohne tools-Feld          Suche unmoeglich

Damit ist die Websuche der einzige Unterschied zwischen den beiden Antworten
auf denselben Prompt. Ausgewertet wird gepaart, ueber die Differenz A-B je
Prompt, mit gepaartem Bootstrap ueber die Prompts.

WAS DIESES SKRIPT AUSDRUECKLICH NICHT IST
-----------------------------------------
Kein Dauerbetriebs-Feature. Es haengt NICHT am Nightly, wird von analyzer/main.py
nicht importiert und schreibt NIEMALS nach data/runs/, sov_history.jsonl oder in
den Snapshot. Ergebnis ist eine einzelne Datei unter data/experiments/. Die
Messreihe bleibt unberuehrt — ein Experiment mit erzwungener Suche gehoert nicht
in eine Zeitreihe, die den Kanal so misst, wie ein Nutzer ihn erlebt.

GLEICHE ZUTATEN WIE DER CRAWL
-----------------------------
  * Prompts     : analyzer.main.load_prompts (exakt dieselbe Ladefunktion,
                  ueber data/config.json -> products[].prompts_file)
  * Client      : analyzer.llm_clients.OpenAIWebSearchClient (kein eigener HTTP-Code;
                  die beiden Arme sind eine Unterklasse, die NUR das tools-Feld
                  der fertigen Payload anfasst — siehe ArmClient)
  * System-Prompt: analyzer.llm_clients.SYSTEM_PROMPT
  * Parameter   : model/temperature/max_tokens/retries aus data/config.json
  * Markenzaehlung: analyzer.metrics.analyse_response — derselbe Matcher wie im
                  Crawl, sonst sind die Zahlen nicht mit dem Projekt vergleichbar.

AUFRUF
------
    python3 tools/search_ab_test.py --dry-run                 # nur Kostenschaetzung + Mock
    python3 tools/search_ab_test.py --limit 120 --repeats 2   # Stichprobe
    python3 tools/search_ab_test.py --repeats 2               # alle 323 Prompts

Kosten siehe --dry-run; die Schaetzung wird vor dem ersten echten Aufruf geloggt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer import llm_clients  # noqa: E402
from analyzer.llm_clients import (  # noqa: E402
    LLMResponse, OpenAIWebSearchClient, SRC_ANNOTATION, SRC_FLIESSTEXT,
    SYSTEM_PROMPT,
)
# Bewusst die Ladefunktion des Crawls, keine eigene: sonst waeren es womoeglich
# andere Prompts als die gemessenen. analyzer/main.py wird nur IMPORTIERT,
# nicht veraendert.
from analyzer.main import load_prompts  # noqa: E402
from analyzer.metrics import BrandSpec, analyse_response  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
EXPERIMENTS_DIR = DATA_DIR / "experiments"

ARM_A = "forced_search"     # tool_choice="required"
ARM_B = "no_tools"          # tools-Feld entfaellt

# --- Preise (Stand 05.08.2026) ---------------------------------------------
# gpt-4.1-mini: 0,40 $ / 1 Mio Input-Token, 1,60 $ / 1 Mio Output-Token.
# Websuche-Werkzeug: 10 $ / 1.000 Aufrufe fuer die aktuellen Modelle. Fuer die
# aelteren *-search-preview-Varianten kursieren 25-30 $ — deshalb ueber
# --web-search-price aenderbar, statt hart verdrahtet.
PRICE_IN_PER_1M = 0.40
PRICE_OUT_PER_1M = 1.60
DEFAULT_WEB_SEARCH_PRICE_PER_1K = 10.0

# Erwartete Token je Aufruf. KEINE Schaetzung aus der Luft, sondern die
# gemessenen Mittelwerte des Laufs 2026-08-05T08-47-32Z, getrennt nach
# Antworten mit und ohne Suchtreffer (n=226 / n=97):
#   mit Suche : 8.174 Input / 695 Output   (Suchergebnisse liegen im Kontext)
#   ohne Suche:   406 Input / 438 Output
EXPECTED_TOKENS = {
    ARM_A: {"in": 8174, "out": 695},
    ARM_B: {"in": 406, "out": 438},
}

MAX_TEXT_CHARS = 20000      # wie im Crawl (analyzer/main.py)


# ===========================================================================
# Die beiden Arme
# ===========================================================================

class ArmClient(OpenAIWebSearchClient):
    """OpenAIWebSearchClient mit zwei Schaltern fuer das Experiment.

    WARUM ALS UNTERKLASSE HIER UND NICHT ALS PARAMETER IN llm_clients.py
    -------------------------------------------------------------------
    Gebraucht werden nur zwei Dinge, die der Nightly nie braucht:
      tool_choice="required"  -> Suche erzwingen   (Arm A)
      use_tools=False         -> tools-Feld weg    (Arm B)
    Der Nightly-Kanal `chatgpt_web` laesst bewusst das Modell entscheiden. Ein
    zusaetzlicher Parameter an der Produktionsklasse waere eine Aenderung an der
    Datei, die die laufende Messreihe erzeugt — fuer ein einmaliges Experiment
    ein schlechtes Tauschgeschaeft. Als Unterklasse kann dieses Skript den
    Nightly nicht kaputtmachen, auch nicht versehentlich.

    ABER: hier wird KEINE Payload nachgebaut. _call_responses ruft das Original
    und veraendert danach genau ein Feld. Aendert sich der Payload-Bau in
    llm_clients.py (Modell, Instruktionen, max_output_tokens, temperature),
    wandert die Aenderung automatisch mit — sonst waere das Experiment schon
    beim naechsten Commit nicht mehr mit dem Crawl vergleichbar.
    """

    def __init__(self, *args, tool_choice=None, use_tools: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_choice = tool_choice
        self.use_tools = bool(use_tools)
        if self.api != "responses":
            raise ValueError(
                "ArmClient braucht api='responses'. Nur die Responses-API kennt "
                "tools/tool_choice; der Chat-Pfad (web_search_options) hat kein "
                "Gegenstueck zu 'required'.")

    def _call_responses(self, prompt: str) -> Dict:
        spec = super()._call_responses(prompt)
        payload = spec["payload"]
        if self.use_tools:
            if self.tool_choice is not None:
                payload["tool_choice"] = self.tool_choice
        else:
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
        return spec


# ===========================================================================
# Konfiguration und Prompts
# ===========================================================================

def load_config() -> Dict:
    return json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))


def build_brands(cfg: Dict) -> Tuple[BrandSpec, List[BrandSpec]]:
    """Wie analyzer/main.py: inkl. extra_domains, sonst zaehlen Zitate der
    Zweitmarken (dkv.de, ergo.com) nicht mit."""
    b = cfg["brand"]
    brand = BrandSpec(
        name=b["name"], aliases=b["aliases"], domain=b["domain"],
        extra_domains=list(b.get("extra_domains") or []),
    )
    competitors = [
        BrandSpec(name=c["name"], aliases=c["aliases"], domain=c["domain"],
                  extra_domains=list(c.get("extra_domains") or []))
        for c in cfg["competitors"]
    ]
    return brand, competitors


def collect_prompts(cfg: Dict) -> List[Dict]:
    """Alle Prompts aller Produkte, jeweils mit Produkt-Zuordnung."""
    out: List[Dict] = []
    for product in cfg["products"]:
        for p in load_prompts(product["prompts_file"]):
            out.append({
                "prompt_id": p["id"],
                "text": p["text"],
                "intent": p.get("intent"),
                "product_id": product["id"],
                "product_name": product["name"],
            })
    return out


def stratified_sample(prompts: List[Dict], limit: int, seed: int) -> List[Dict]:
    """Zufallsstichprobe mit festem Seed, geschichtet nach Produkt.

    Ohne Schichtung koennte die Stichprobe ganze Produkte auslassen — genau die
    Achse, entlang der die Suchquote am staerksten schwankt (Reise 87 %,
    Risikoleben 43 %). Die Aufteilung folgt den Produktgroessen (Largest
    Remainder), damit die Stichprobe dieselbe Produktmischung hat wie der Crawl.
    """
    if limit <= 0 or limit >= len(prompts):
        return list(prompts)

    by_product: Dict[str, List[Dict]] = {}
    for p in prompts:
        by_product.setdefault(p["product_id"], []).append(p)

    total = len(prompts)
    exact = {pid: limit * len(items) / total for pid, items in by_product.items()}
    quota = {pid: int(math.floor(v)) for pid, v in exact.items()}
    rest = limit - sum(quota.values())
    # Largest Remainder: die Produkte mit dem groessten abgeschnittenen
    # Nachkommateil bekommen die Restplaetze. Bei Gleichstand entscheidet die
    # Produkt-ID, damit das Ergebnis reproduzierbar bleibt.
    order = sorted(by_product, key=lambda pid: (-(exact[pid] - quota[pid]), pid))
    for pid in order[:max(0, rest)]:
        quota[pid] += 1

    rng = random.Random(seed)
    picked: List[Dict] = []
    for pid in sorted(by_product):
        items = sorted(by_product[pid], key=lambda x: x["prompt_id"])
        k = min(quota.get(pid, 0), len(items))
        picked.extend(rng.sample(items, k) if k else [])
    picked.sort(key=lambda x: (x["product_id"], x["prompt_id"]))
    return picked


# ===========================================================================
# Kostenschaetzung
# ===========================================================================

def estimate_cost(n_prompts: int, repeats: int, web_search_price_per_1k: float) -> Dict:
    calls_per_arm = n_prompts * repeats
    def _tok(arm):
        t = EXPECTED_TOKENS[arm]
        return (t["in"] * PRICE_IN_PER_1M + t["out"] * PRICE_OUT_PER_1M) / 1e6
    cost_a_tokens = calls_per_arm * _tok(ARM_A)
    cost_b_tokens = calls_per_arm * _tok(ARM_B)
    # In Arm A erzwingt tool_choice="required" mindestens einen Suchaufruf je
    # Antwort — die Werkzeug-Gebuehr faellt also bei JEDEM Aufruf an, nicht nur
    # bei 70 %. Arm B hat keine tools, dort faellt sie nie an.
    cost_search = calls_per_arm * (web_search_price_per_1k / 1000.0)
    return {
        "calls_arm_a": calls_per_arm,
        "calls_arm_b": calls_per_arm,
        "calls_total": 2 * calls_per_arm,
        "usd_tokens_arm_a": round(cost_a_tokens, 2),
        "usd_tokens_arm_b": round(cost_b_tokens, 2),
        "usd_web_search": round(cost_search, 2),
        "usd_total": round(cost_a_tokens + cost_b_tokens + cost_search, 2),
        "web_search_price_per_1k": web_search_price_per_1k,
        "annahme_tokens": EXPECTED_TOKENS,
        "hinweis": ("Token-Mittelwerte aus Lauf 2026-08-05T08-47-32Z. Die "
                    "tatsaechlichen Kosten stehen nach dem Lauf im Feld "
                    "cost_actual (aus den gemeldeten usage-Token)."),
    }


# ===========================================================================
# Mock-Transport fuer --dry-run  (kein Netz, echtes Response-Schema)
# ===========================================================================

class _MockHTTPResponse:
    def __init__(self, payload: Dict):
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)[:400]

    def json(self) -> Dict:
        return self._payload


class MockOpenAI:
    """Erzeugt Antworten im echten Schema der Responses-API.

    Zweck: im Container gibt es keinen OPENAI_API_KEY. Der Mock haengt sich an
    dieselbe Stelle wie das Netz (requests.post in analyzer.llm_clients) und
    laesst damit den KOMPLETTEN echten Codepfad laufen: Payload-Bau in
    _call_responses, Parser _parse_responses, Annotations-Auswertung,
    Fliesstext-Fallback, Retry-Wrapper. Getestet wird also das Skript, nicht
    eine Attrappe davon.

    Der Mock ist bewusst so gebaut, dass Arm A haeufiger ERGO nennt als Arm B —
    sonst liefe die Bootstrap-Auswertung im Trockenlauf gegen einen Nulleffekt
    und man saehe nicht, ob sie ueberhaupt etwas findet.
    """

    def __init__(self):
        self.calls: List[Dict] = []

    @staticmethod
    def _rand(seed_text: str) -> random.Random:
        h = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
        return random.Random(int(h[:16], 16))

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        payload = json or {}
        self.calls.append(payload)
        prompt = payload.get("input") or ""
        has_tools = "tools" in payload
        tool_choice = payload.get("tool_choice")
        # Jeder Aufruf leicht anders (Wiederholungen sind nicht deterministisch)
        rng = self._rand(f"{prompt}|{has_tools}|{tool_choice}|{len(self.calls)}")

        brands = ["Allianz", "AXA", "HUK-Coburg", "Generali", "DEVK", "R+V"]
        rng.shuffle(brands)
        ergo_p = 0.55 if has_tools else 0.22     # erwarteter Effekt im Mock
        nennt_ergo = rng.random() < ergo_p
        liste = brands[:4]
        if nennt_ergo:
            liste.insert(rng.randrange(0, 3), "ERGO")

        zeilen = ["Hier eine Einordnung der Anbieter:"]
        zeilen += [f"{i+1}. {b} - solide Konditionen." for i, b in enumerate(liste)]
        if nennt_ergo and rng.random() < 0.4:
            zeilen.append("Die ERGO bietet zusaetzlich einen Sofortschutz an.")

        output: List[Dict] = []
        annotations: List[Dict] = []
        if has_tools:
            output.append({"type": "web_search_call", "id": "ws_mock",
                           "status": "completed"})
            # In ~8 % der Faelle liefert das Modell trotz erzwungener Suche
            # keine Zitate — genau die Gegenprobe, die das Skript ausweisen soll.
            if rng.random() > 0.08:
                zeilen.append("")
                zeilen.append("Quellen:")
                for dom in ["ergo.de", "allianz.de", "test.de"][:rng.randint(1, 3)]:
                    u = f"https://www.{dom}/vergleich"
                    zeilen.append(u)
                    annotations.append({"type": "url_citation", "url": u,
                                        "title": f"Vergleich | {dom}"})
        else:
            # Arm B nennt gelegentlich von sich aus URLs im Fliesstext —
            # unbelegt, aber vorhanden. Auch das ist eine Gegenprobe.
            if rng.random() < 0.35:
                zeilen.append("Mehr dazu unter https://www.ergo.de/ratgeber")

        text = "\n".join(zeilen)
        tok = EXPECTED_TOKENS[ARM_A if has_tools else ARM_B]
        data = {
            "id": "resp_mock",
            "model": payload.get("model"),
            "output": output + [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text,
                             "annotations": annotations}],
            }],
            "usage": {
                "input_tokens": int(tok["in"] * rng.uniform(0.8, 1.2)),
                "output_tokens": int(tok["out"] * rng.uniform(0.8, 1.2)),
            },
        }
        time.sleep(0.001)
        return _MockHTTPResponse(data)


class _mock_transport:
    """Haengt MockOpenAI.post an analyzer.llm_clients.requests.post und raeumt
    danach wieder auf. Nur fuer --dry-run; im Produktivpfad passiert das nie."""

    def __init__(self):
        self.mock = MockOpenAI()
        self._orig = None

    def __enter__(self) -> MockOpenAI:
        self._orig = llm_clients.requests.post
        llm_clients.requests.post = self.mock.post
        return self.mock

    def __exit__(self, *exc):
        llm_clients.requests.post = self._orig
        return False


# ===========================================================================
# Aufrufe
# ===========================================================================

def build_arm_clients(cfg: Dict, model: Optional[str], tool_choice: str,
                      api_key: str) -> Dict[str, ArmClient]:
    """Zwei Clients, identisch bis auf das Werkzeug."""
    llm_cfg = next((l for l in cfg.get("llms", []) if l.get("id") == "chatgpt_web"), {})
    st = cfg.get("settings", {}) or {}
    common = dict(
        api_key=api_key,
        model=model or llm_cfg.get("model") or "gpt-4.1-mini",
        # Responses-API erzwingen: nur dort gibt es tools/tool_choice. Der
        # Chat-Pfad (web_search_options) kennt kein Gegenstueck zu "required".
        api="responses",
        search_context_size=llm_cfg.get("search_context_size", "low"),
        temperature=float(st.get("temperature", 0.3)),
        max_tokens=int(st.get("max_tokens", 1200)),
        retries=min(int(st.get("retry_attempts", 3) or 3), 5),
    )
    return {
        ARM_A: ArmClient(tool_choice=tool_choice, use_tools=True, **common),
        ARM_B: ArmClient(use_tools=False, **common),
    }


def ask_one(client, prompt_text: str) -> LLMResponse:
    """Einzelfehler duerfen den Lauf nie beenden — der Client faengt bereits
    alles ab, das hier ist der Guertel zum Hosentraeger."""
    try:
        return client.ask(prompt_text)
    except Exception as e:  # noqa: BLE001
        return LLMResponse(text="", sources=[], model="?", latency_ms=0.0,
                           error=str(e)[:500])


def run_calls(prompts: List[Dict], clients: Dict, repeats: int, parallel: int,
              brand: BrandSpec, competitors: List[BrandSpec]) -> List[Dict]:
    tasks = [(p, arm, rep)
             for p in prompts
             for arm in (ARM_A, ARM_B)
             for rep in range(1, repeats + 1)]
    records: List[Dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {pool.submit(ask_one, clients[arm], p["text"]): (p, arm, rep)
                   for p, arm, rep in tasks}
        for fut in as_completed(futures):
            p, arm, rep = futures[fut]
            try:
                resp = fut.result()
            except Exception as e:  # noqa: BLE001
                resp = LLMResponse(text="", sources=[], model="?",
                                   latency_ms=0.0, error=str(e)[:500])
            records.append(make_record(p, arm, rep, resp, brand, competitors))
            done += 1
            if done % 25 == 0 or done == len(tasks):
                fehler = sum(1 for r in records if r["error"])
                print(f"  [CALL] {done}/{len(tasks)} fertig ({fehler} Fehler)")
    records.sort(key=lambda r: (r["product_id"], r["prompt_id"], r["arm"], r["repeat"]))
    return records


def make_record(p: Dict, arm: str, rep: int, resp: LLMResponse,
                brand: BrandSpec, competitors: List[BrandSpec]) -> Dict:
    text = resp.text or ""
    sources = resp.sources or []
    metrics = analyse_response(text, sources, brand, competitors)
    ergo = next(b for b in metrics["brands"] if b["name"] == brand.name)
    n_ann = sum(1 for s in sources if s.get("src_typ") == SRC_ANNOTATION)
    n_flie = sum(1 for s in sources if s.get("src_typ") == SRC_FLIESSTEXT)
    return {
        "prompt_id": p["prompt_id"],
        "product_id": p["product_id"],
        "product_name": p["product_name"],
        "arm": arm,
        "repeat": rep,
        # Erfolgskriterium wie in metrics.aggregate_product_metrics:
        # Fehler oder leerer Text zaehlen nicht mit.
        "ok": bool(not resp.error and text.strip()),
        "error": resp.error,
        "latency_ms": resp.latency_ms,
        "tokens_in": resp.tokens_in,
        "tokens_out": resp.tokens_out,
        "response_text": text[:MAX_TEXT_CHARS],
        "text_truncated": len(text) > MAX_TEXT_CHARS,
        "sources": sources,
        "n_sources_annotation": n_ann,
        "n_sources_fliesstext": n_flie,
        "ergo_mentions": ergo["mentions"],
        "ergo_mentioned": bool(ergo["mentioned"]),
        "ergo_first_rank": ergo["first_rank"],
        "ergo_sov": ergo["share_of_voice"],
        "ergo_cited": bool(ergo["cited"]),
        "total_mentions": metrics["total_mentions"],
    }


# ===========================================================================
# Gepaarte Auswertung
# ===========================================================================

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def pair_by_prompt(records: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Je Prompt die Wiederholungen mitteln und A gegen B stellen.

    Ein Prompt kommt nur ins Paar, wenn BEIDE Arme mindestens eine verwertbare
    Antwort haben. Sonst waere die Differenz keine Differenz, sondern ein
    halber Messpunkt — genau der Fehler, den dieses Experiment vermeiden soll.
    """
    by_key: Dict[Tuple[str, str], Dict[str, List[Dict]]] = {}
    for r in records:
        key = (r["product_id"], r["prompt_id"])
        by_key.setdefault(key, {ARM_A: [], ARM_B: []})[r["arm"]].append(r)

    pairs, dropped = [], []
    for (pid, prompt_id), arms in sorted(by_key.items()):
        ok_a = [r for r in arms[ARM_A] if r["ok"]]
        ok_b = [r for r in arms[ARM_B] if r["ok"]]
        if not ok_a or not ok_b:
            dropped.append({
                "product_id": pid, "prompt_id": prompt_id,
                "ok_arm_a": len(ok_a), "ok_arm_b": len(ok_b),
                "grund": "mindestens ein Arm ohne verwertbare Antwort",
            })
            continue
        side = {}
        for arm, rs in ((ARM_A, ok_a), (ARM_B, ok_b)):
            side[arm] = {
                "n_ok": len(rs),
                "mentions": _mean([r["ergo_mentions"] for r in rs]),
                "mentioned": _mean([1.0 if r["ergo_mentioned"] else 0.0 for r in rs]),
                "sov": _mean([r["ergo_sov"] for r in rs]),
                "rank": _mean([r["ergo_first_rank"] for r in rs
                               if r["ergo_first_rank"] is not None]),
                "cited": _mean([1.0 if r["ergo_cited"] else 0.0 for r in rs]),
                # Summen fuer den gepoolten SoV (so rechnet der Crawl)
                "sum_ergo_mentions": sum(r["ergo_mentions"] for r in rs),
                "sum_total_mentions": sum(r["total_mentions"] for r in rs),
            }
        a, b = side[ARM_A], side[ARM_B]
        pairs.append({
            "product_id": pid,
            "prompt_id": prompt_id,
            "a": a, "b": b,
            "diff": {
                "mentions": a["mentions"] - b["mentions"],
                "mentioned": a["mentioned"] - b["mentioned"],
                "sov": a["sov"] - b["sov"],
                # Rang nur, wenn ERGO in BEIDEN Armen ueberhaupt in einer Liste
                # auftaucht. Negativ = in Arm A weiter vorn (Rang 1 ist besser).
                "rank": (a["rank"] - b["rank"])
                        if (a["rank"] is not None and b["rank"] is not None) else None,
                "cited": a["cited"] - b["cited"],
            },
        })
    return pairs, dropped


def _pooled_sov(pairs: List[Dict], idx: List[int], arm_key: str) -> float:
    ergo = sum(pairs[i][arm_key]["sum_ergo_mentions"] for i in idx)
    total = sum(pairs[i][arm_key]["sum_total_mentions"] for i in idx)
    return (ergo / total) if total else 0.0


def paired_bootstrap(pairs: List[Dict], n_boot: int, seed: int,
                     alpha: float = 0.05) -> Dict:
    """Gepaarter Bootstrap ueber die PROMPTS.

    Gezogen werden ganze Prompt-Paare mit Zuruecklegen; jede Ziehung nimmt beide
    Arme desselben Prompts mit. Damit traegt die Ziehung die Prompt-Auswahl als
    Unsicherheitsquelle korrekt mit — die Frage ist ja "was wuerde bei einer
    anderen Prompt-Menge herauskommen", nicht "bei denselben Prompts nochmal".
    Ausgewiesen wird ein Perzentil-Intervall.
    """
    n = len(pairs)
    out: Dict = {"n_pairs": n, "n_bootstrap": n_boot, "alpha": alpha}
    if n == 0:
        return out

    keys = ["mentions", "mentioned", "sov", "cited"]
    obs = {k: statistics.fmean([p["diff"][k] for p in pairs]) for k in keys}
    rank_pairs = [p["diff"]["rank"] for p in pairs if p["diff"]["rank"] is not None]
    obs["rank"] = statistics.fmean(rank_pairs) if rank_pairs else None

    all_idx = list(range(n))
    obs_pooled_a = _pooled_sov(pairs, all_idx, "a")
    obs_pooled_b = _pooled_sov(pairs, all_idx, "b")
    obs["sov_pooled"] = obs_pooled_a - obs_pooled_b

    rng = random.Random(seed)
    draws: Dict[str, List[float]] = {k: [] for k in list(obs) if obs[k] is not None}
    rank_idx = [i for i, p in enumerate(pairs) if p["diff"]["rank"] is not None]
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        for k in keys:
            draws[k].append(statistics.fmean([pairs[i]["diff"][k] for i in idx]))
        if "sov_pooled" in draws:
            draws["sov_pooled"].append(
                _pooled_sov(pairs, idx, "a") - _pooled_sov(pairs, idx, "b"))
        if "rank" in draws and rank_idx:
            ridx = [rank_idx[rng.randrange(len(rank_idx))] for _ in rank_idx]
            draws["rank"].append(
                statistics.fmean([pairs[i]["diff"]["rank"] for i in ridx]))

    def _ci(vals):
        if not vals:
            return (None, None)
        s = sorted(vals)
        lo = s[max(0, int(math.floor((alpha / 2) * len(s))))]
        hi = s[min(len(s) - 1, int(math.ceil((1 - alpha / 2) * len(s))) - 1)]
        return (lo, hi)

    metrics: Dict[str, Dict] = {}
    for k, val in obs.items():
        if val is None:
            metrics[k] = {"diff": None, "ci_low": None, "ci_high": None,
                          "n": 0, "hinweis": "in keinem Paar in beiden Armen vorhanden"}
            continue
        lo, hi = _ci(draws.get(k, []))
        metrics[k] = {
            "diff": round(val, 5),
            "ci_low": round(lo, 5) if lo is not None else None,
            "ci_high": round(hi, 5) if hi is not None else None,
            "n": len(rank_pairs) if k == "rank" else n,
            "ci_excludes_zero": bool(lo is not None and hi is not None
                                     and (lo > 0 or hi < 0)),
        }

    metrics["sov_pooled"]["arm_a"] = round(obs_pooled_a, 5)
    metrics["sov_pooled"]["arm_b"] = round(obs_pooled_b, 5)
    for k in keys:
        metrics[k]["arm_a"] = round(statistics.fmean([p["a"][k] for p in pairs]), 5)
        metrics[k]["arm_b"] = round(statistics.fmean([p["b"][k] for p in pairs]), 5)
    if rank_pairs:
        ra = [p["a"]["rank"] for p in pairs if p["diff"]["rank"] is not None]
        rb = [p["b"]["rank"] for p in pairs if p["diff"]["rank"] is not None]
        metrics["rank"]["arm_a"] = round(statistics.fmean(ra), 5)
        metrics["rank"]["arm_b"] = round(statistics.fmean(rb), 5)

    out["metrics"] = metrics
    out["permutation_p"] = sign_flip_p(
        [p["diff"]["sov"] for p in pairs], n_boot, seed + 1)
    out["permutation_p_hinweis"] = (
        "Zweiseitiger Vorzeichen-Permutationstest auf die mittlere gepaarte "
        "SoV-Differenz. Steht bewusst NEBEN dem Konfidenzintervall, nicht "
        "an dessen Stelle: der p-Wert sagt nichts ueber die Groesse des Effekts."
    )
    return out


def sign_flip_p(diffs: List[float], n_perm: int, seed: int) -> Optional[float]:
    diffs = [d for d in diffs if d is not None]
    if not diffs:
        return None
    obs = abs(statistics.fmean(diffs))
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_perm):
        m = statistics.fmean([d if rng.random() < 0.5 else -d for d in diffs])
        if abs(m) >= obs - 1e-12:
            hits += 1
    return round((hits + 1) / (n_perm + 1), 5)


def by_product(pairs: List[Dict], n_boot: int, seed: int) -> Dict[str, Dict]:
    """Dieselbe gepaarte Rechnung je Produkt. Die Suchquote schwankt stark nach
    Produkt (Reise 87 %, Risikoleben 43 %) — falls die Websuche dort
    unterschiedlich wirkt, muss man das sehen koennen. Achtung: rund 30 Prompts
    je Produkt ergeben breite Intervalle."""
    groups: Dict[str, List[Dict]] = {}
    for p in pairs:
        groups.setdefault(p["product_id"], []).append(p)
    return {
        pid: paired_bootstrap(items, max(1000, n_boot // 4), seed + i)
        for i, (pid, items) in enumerate(sorted(groups.items()))
    }


def counter_checks(records: List[Dict]) -> Dict:
    """Die zwei Gegenproben: Hat 'required' wirklich gesucht, und hat der
    tool-freie Arm wirklich nicht?"""
    a = [r for r in records if r["arm"] == ARM_A and r["ok"]]
    b = [r for r in records if r["arm"] == ARM_B and r["ok"]]
    a_ohne = [r for r in a if r["n_sources_annotation"] == 0]
    b_mit_urls = [r for r in b if r["n_sources_fliesstext"] > 0]
    return {
        "arm_a_ok": len(a),
        "arm_a_ohne_quellen": len(a_ohne),
        "arm_a_ohne_quellen_rate": round(len(a_ohne) / len(a), 4) if a else None,
        "arm_a_ohne_quellen_beispiele": sorted(
            {r["prompt_id"] for r in a_ohne})[:25],
        "arm_a_hinweis": (
            "tool_choice='required' erzwingt den Werkzeugaufruf, nicht das "
            "Zitat. Antworten ohne url_citation sind moeglich — je hoeher diese "
            "Quote, desto schwaecher trennt das Experiment die beiden Arme."),
        "arm_b_ok": len(b),
        "arm_b_mit_fliesstext_urls": len(b_mit_urls),
        "arm_b_mit_fliesstext_urls_rate": round(len(b_mit_urls) / len(b), 4) if b else None,
        "arm_b_mit_fliesstext_urls_beispiele": sorted(
            {r["prompt_id"] for r in b_mit_urls})[:25],
        "arm_b_hinweis": (
            "Ohne tools kann das Modell nicht suchen. URLs im Fliesstext stammen "
            "aus dem Modellgedaechtnis und koennen erfunden sein — sie zaehlen in "
            "metrics.cited_brand trotzdem als Zitat. Deshalb ist 'cited' im "
            "Vergleich der beiden Arme die unzuverlaessigste der Kennzahlen."),
    }


def actual_cost(records: List[Dict], web_search_price_per_1k: float) -> Dict:
    tin = sum(r["tokens_in"] or 0 for r in records)
    tout = sum(r["tokens_out"] or 0 for r in records)
    # Gebuehr nur fuer Arm A und nur fuer Antworten, die tatsaechlich Zitate
    # lieferten — mehr laesst sich aus der API-Antwort nicht ablesen. Ein
    # Suchaufruf ohne Zitat wuerde hier fehlen, die Zahl ist also eine
    # Untergrenze.
    searched = sum(1 for r in records
                   if r["arm"] == ARM_A and r["n_sources_annotation"] > 0)
    return {
        "tokens_in": tin,
        "tokens_out": tout,
        "usd_tokens": round((tin * PRICE_IN_PER_1M + tout * PRICE_OUT_PER_1M) / 1e6, 2),
        "web_search_calls_mind": searched,
        "usd_web_search_mind": round(searched * web_search_price_per_1k / 1000.0, 2),
        "usd_total_mind": round(
            (tin * PRICE_IN_PER_1M + tout * PRICE_OUT_PER_1M) / 1e6
            + searched * web_search_price_per_1k / 1000.0, 2),
        "hinweis": "Untergrenze: Suchaufrufe ohne Zitat sind aus der Antwort nicht erkennbar.",
    }


# ===========================================================================
# Ausgabe
# ===========================================================================

def _pct(x, digits=2):
    return "-" if x is None else f"{x * 100:.{digits}f} %"


def _ci_str(m, as_pct=True, digits=2):
    """Differenzen immer MIT Vorzeichen: bei Prozentpunkten ist '8,32' ohne
    Vorzeichen nicht von einem Niveau zu unterscheiden."""
    if m.get("diff") is None:
        return "-"
    f = ((lambda v: f"{v * 100:+.{digits}f} pp") if as_pct
         else (lambda v: f"{v:+.2f}"))
    return f"{f(m['diff'])}  [{f(m['ci_low'])}; {f(m['ci_high'])}]"


def write_markdown(result: Dict, path: Path) -> None:
    b = result["bootstrap"]
    m = b.get("metrics", {})
    cc = result["counter_checks"]
    L: List[str] = []
    L.append(f"# Websuche-Experiment (gepaart) - {result['experiment_id']}")
    L.append("")
    if result["dry_run"]:
        L.append("> **DRY-RUN.** Antworten stammen aus dem Mock, nicht von OpenAI. "
                 "Die Zahlen unten sind Attrappen und belegen nichts.")
        L.append("")
    L.append(f"- Modell: `{result['model']}` (Responses-API), "
             f"temperature {result['params']['temperature']}, "
             f"max_tokens {result['params']['max_tokens']}")
    L.append(f"- Arm A: `tool_choice: {result['params']['tool_choice']}` (Suche erzwungen)")
    L.append("- Arm B: ohne `tools` (Suche unmoeglich)")
    L.append(f"- Prompts: {result['n_prompts']} von {result['n_prompts_available']}"
             + (f" (Stichprobe, seed {result['seed']}, geschichtet nach Produkt)"
                if result['limit'] else " (alle)"))
    L.append(f"- Wiederholungen je Prompt und Arm: {result['repeats']} "
             f"-> {result['n_calls']} Aufrufe")
    L.append(f"- Auswertbare Paare: {b.get('n_pairs', 0)}"
             + (f", verworfen: {len(result['dropped_pairs'])}"
                if result["dropped_pairs"] else ""))
    L.append("")
    L.append("## Ergebnis: Differenz A minus B, gepaart ueber die Prompts")
    L.append("")
    L.append("95-%-Perzentilintervalle aus gepaartem Bootstrap "
             f"({b.get('n_bootstrap', 0)} Ziehungen ganzer Prompt-Paare).")
    L.append("")
    L.append("| Kennzahl | Arm A (Suche) | Arm B (ohne) | Differenz [95 % KI] | KI ohne Null |")
    L.append("|---|---|---|---|---|")
    rows = [
        ("SoV (gepoolt, wie im Crawl)", "sov_pooled", True),
        ("SoV (Mittel je Prompt)", "sov", True),
        ("genannt in", "mentioned", True),
        ("ERGO-Nennungen je Antwort", "mentions", False),
        ("Rang (kleiner = besser)", "rank", False),
        ("zitiert", "cited", True),
    ]
    for label, key, as_pct in rows:
        mm = m.get(key)
        if not mm:
            continue
        aval = mm.get("arm_a")
        bval = mm.get("arm_b")
        fa = _pct(aval) if as_pct else (f"{aval:.2f}" if aval is not None else "-")
        fb = _pct(bval) if as_pct else (f"{bval:.2f}" if bval is not None else "-")
        flag = "ja" if mm.get("ci_excludes_zero") else "nein"
        L.append(f"| {label} | {fa} | {fb} | {_ci_str(mm, as_pct)} | {flag} |")
    L.append("")
    L.append(f"Vorzeichen-Permutationstest auf die mittlere gepaarte SoV-Differenz: "
             f"p = {b.get('permutation_p')}. Der p-Wert steht neben dem Intervall, "
             f"nicht an dessen Stelle.")
    L.append("")
    L.append("## Je Produkt")
    L.append("")
    L.append("| Produkt | Paare | SoV gepoolt A | B | Differenz [95 % KI] |")
    L.append("|---|---|---|---|---|")
    for pid, pb in result["by_product"].items():
        pm = (pb.get("metrics") or {}).get("sov_pooled")
        if not pm:
            continue
        L.append(f"| {pid} | {pb.get('n_pairs', 0)} | {_pct(pm.get('arm_a'))} | "
                 f"{_pct(pm.get('arm_b'))} | {_ci_str(pm)} |")
    L.append("")
    L.append("Rund 30 Prompts je Produkt: diese Intervalle sind breit. "
             "Sie taugen zum Ausschliessen grosser Unterschiede zwischen Produkten, "
             "nicht zum Rangieren der Produkte untereinander.")
    L.append("")
    L.append("## Gegenproben")
    L.append("")
    L.append(f"- Arm A ohne jede ausgewiesene Quelle: "
             f"{cc['arm_a_ohne_quellen']} von {cc['arm_a_ok']} "
             f"({_pct(cc['arm_a_ohne_quellen_rate'])}). {cc['arm_a_hinweis']}")
    L.append(f"- Arm B mit selbst genannten URLs im Fliesstext: "
             f"{cc['arm_b_mit_fliesstext_urls']} von {cc['arm_b_ok']} "
             f"({_pct(cc['arm_b_mit_fliesstext_urls_rate'])}). {cc['arm_b_hinweis']}")
    L.append("")
    L.append("## Fehler")
    L.append("")
    e = result["errors"]
    L.append(f"- Fehlgeschlagene Aufrufe: {e['n_failed']} von {result['n_calls']} "
             f"(Arm A {e['arm_a']}, Arm B {e['arm_b']})")
    for msg, cnt in list(e["top_messages"].items())[:5]:
        L.append(f"  - {cnt}x `{msg}`")
    L.append("")
    L.append("## Kosten")
    L.append("")
    est, act = result["cost_estimate"], result.get("cost_actual") or {}
    L.append(f"- Schaetzung vorab: {est['usd_total']:.2f} $ "
             f"({est['calls_total']} Aufrufe)")
    if act:
        L.append(f"- Tatsaechlich (Untergrenze): {act['usd_total_mind']:.2f} $ "
                 f"— {act['tokens_in']} Input-, {act['tokens_out']} Output-Token, "
                 f"mind. {act['web_search_calls_mind']} Suchaufrufe")
    L.append("")
    L.append("## Lesehilfe")
    L.append("")
    L.append("Der unkontrollierte Vergleich vom 05.08.2026 (10,54 % gegen 3,16 % SoV) "
             "vergleicht zwei verschiedene Fragenmengen: das Modell entscheidet selbst, "
             "wann es sucht, und sucht bevorzugt bei anbieterlastigen Fragen. Dieses "
             "Experiment stellt dieselben Prompts unter beide Bedingungen. Die Differenz "
             "oben ist damit die Wirkung der Websuche; die Luecke zu 7,4 Prozentpunkten "
             "ist der Anteil, den die Selbstauswahl des Modells erklaert hat.")
    L.append("")
    L.append("Grenzen: erzwungene Suche ist nicht das, was ein Nutzer erlebt "
             "(dort sucht das Modell in 70 % der Faelle). Das Experiment misst den "
             "Effekt der Suche, nicht die reale Sichtbarkeit des Kanals. Es gehoert "
             "deshalb nicht in die Zeitreihe und beruehrt data/runs nicht.")
    L.append("")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ===========================================================================
# main
# ===========================================================================

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Gepaartes A/B-Experiment: erzwungene Websuche gegen keine Tools.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Zufallsstichprobe von N Prompts insgesamt, geschichtet "
                         "nach Produkt (0 = alle).")
    ap.add_argument("--seed", type=int, default=20260805,
                    help="Seed fuer Stichprobe und Bootstrap (Default 20260805).")
    ap.add_argument("--repeats", type=int, default=2,
                    help="Wiederholungen je Prompt und Arm (Default 2).")
    ap.add_argument("--max-calls", type=int, default=1400,
                    help="Harte Obergrenze fuer die Zahl der API-Aufrufe. Wird sie "
                         "ueberschritten, bricht das Skript VOR dem ersten Aufruf ab.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Keine echten API-Aufrufe. Zeigt die Kostenschaetzung und "
                         "laeuft mit Mock-Antworten im echten Response-Schema durch.")
    ap.add_argument("--tool-choice", default="required", choices=["required", "auto"],
                    help="tool_choice fuer Arm A (Default required).")
    ap.add_argument("--model", default=None,
                    help="Modell (Default: das aus config.json fuer chatgpt_web).")
    ap.add_argument("--parallel", type=int, default=0,
                    help="Parallele Aufrufe (Default: settings.parallel_requests).")
    ap.add_argument("--bootstrap", type=int, default=5000,
                    help="Bootstrap-Ziehungen (Default 5000).")
    ap.add_argument("--web-search-price", type=float,
                    default=DEFAULT_WEB_SEARCH_PRICE_PER_1K,
                    help="Preis der Websuche in $ je 1.000 Aufrufe (Default 10; fuer "
                         "aeltere *-search-preview-Modelle 25-30).")
    ap.add_argument("--out-dir", default=str(EXPERIMENTS_DIR),
                    help="Zielverzeichnis (Default data/experiments).")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    out_dir = Path(args.out_dir).resolve()
    # Schutzgurt: dieses Skript darf die Messreihe nicht anfassen. Ein Tippfehler
    # in --out-dir wuerde sonst reichen, um data/runs zu beschaedigen.
    verboten = {(DATA_DIR / "runs").resolve(), (DATA_DIR / "snapshots").resolve(),
                (DATA_DIR / "pages").resolve()}
    if out_dir in verboten or any(v in out_dir.parents for v in verboten):
        print(f"[ABBRUCH] --out-dir {out_dir} liegt in der Messreihe. "
              f"Das Experiment schreibt ausschliesslich nach data/experiments.")
        return 2

    cfg = load_config()
    brand, competitors = build_brands(cfg)
    alle_prompts = collect_prompts(cfg)
    prompts = stratified_sample(alle_prompts, args.limit, args.seed) \
        if args.limit else alle_prompts

    repeats = max(1, args.repeats)
    est = estimate_cost(len(prompts), repeats, args.web_search_price)

    print("=" * 72)
    print("GEPAARTES WEBSUCHE-EXPERIMENT")
    print("=" * 72)
    print(f"Prompts        : {len(prompts)} von {len(alle_prompts)}"
          + (f"  (Stichprobe, seed {args.seed}, geschichtet nach Produkt)"
             if args.limit else "  (alle)"))
    verteilung: Dict[str, int] = {}
    for p in prompts:
        verteilung[p["product_id"]] = verteilung.get(p["product_id"], 0) + 1
    print(f"  je Produkt   : " + ", ".join(f"{k} {v}" for k, v in sorted(verteilung.items())))
    print(f"Wiederholungen : {repeats} je Prompt und Arm")
    print(f"Arm A          : tool_choice={args.tool_choice!r}, tools=[web_search]")
    print(f"Arm B          : ohne tools")
    print("-" * 72)
    print("[KOSTEN] Schaetzung VOR dem ersten Aufruf")
    print(f"  Aufrufe            : {est['calls_total']} "
          f"({est['calls_arm_a']} A + {est['calls_arm_b']} B)")
    print(f"  Token Arm A        : {est['usd_tokens_arm_a']:.2f} $")
    print(f"  Token Arm B        : {est['usd_tokens_arm_b']:.2f} $")
    print(f"  Websuche-Gebuehr   : {est['usd_web_search']:.2f} $ "
          f"({args.web_search_price:.0f} $/1.000, jeder A-Aufruf sucht)")
    print(f"  SUMME              : {est['usd_total']:.2f} $")
    print("-" * 72)

    if est["calls_total"] > args.max_calls:
        print(f"[ABBRUCH] {est['calls_total']} geplante Aufrufe ueberschreiten "
              f"--max-calls {args.max_calls}. Kein einziger Aufruf wurde gemacht.")
        print(f"          Entweder --limit senken, --repeats senken oder "
              f"--max-calls bewusst anheben.")
        return 2

    api_key = os.getenv("OPENAI_API_KEY")
    if not args.dry_run and not api_key:
        print("[ABBRUCH] OPENAI_API_KEY fehlt. Mit --dry-run laeuft alles gegen Mocks.")
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    st = cfg.get("settings", {}) or {}
    parallel = args.parallel or int(st.get("parallel_requests", 5))

    t0 = time.perf_counter()
    if args.dry_run:
        print("[DRY-RUN] Keine echten API-Aufrufe. Mock-Antworten im Schema der "
              "Responses-API, echter Client- und Parser-Pfad.")
        with _mock_transport() as mock:
            clients = build_arm_clients(cfg, args.model, args.tool_choice,
                                        api_key or "mock-key")
            records = run_calls(prompts, clients, repeats, parallel, brand, competitors)
        # Payload-Kontrolle: hat Arm A wirklich required gesetzt und Arm B
        # wirklich keine tools? Im Trockenlauf ist das die einzige Stelle, an
        # der sich das ueberhaupt pruefen laesst.
        a_payloads = [c for c in mock.calls if "tools" in c]
        b_payloads = [c for c in mock.calls if "tools" not in c]
        print(f"[DRY-RUN] Payload-Kontrolle: {len(a_payloads)} Aufrufe mit tools "
              f"(tool_choice={sorted({str(c.get('tool_choice')) for c in a_payloads})}), "
              f"{len(b_payloads)} ohne tools "
              f"(tool_choice gesetzt: {any('tool_choice' in c for c in b_payloads)})")
    else:
        clients = build_arm_clients(cfg, args.model, args.tool_choice, api_key)
        print(f"[LAUF] Starte {est['calls_total']} Aufrufe, {parallel} parallel ...")
        records = run_calls(prompts, clients, repeats, parallel, brand, competitors)
    dauer = time.perf_counter() - t0

    pairs, dropped = pair_by_prompt(records)
    boot = paired_bootstrap(pairs, args.bootstrap, args.seed)
    prod = by_product(pairs, args.bootstrap, args.seed)

    failed = [r for r in records if not r["ok"]]
    msgs: Dict[str, int] = {}
    for r in failed:
        key = (r["error"] or "leere Antwort")[:120]
        msgs[key] = msgs.get(key, 0) + 1

    modell = next((l for l in cfg.get("llms", []) if l.get("id") == "chatgpt_web"), {})
    result = {
        "experiment_id": ts,
        "experiment": "search_ab_paired",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
        "hinweis": ("Kontrolliertes Experiment mit ERZWUNGENER Websuche. Gehoert "
                    "NICHT in die Zeitreihe: data/runs, sov_history.jsonl und die "
                    "Snapshots werden von diesem Skript nie beschrieben."),
        "model": args.model or modell.get("model") or "gpt-4.1-mini",
        "system_prompt": SYSTEM_PROMPT,
        "params": {
            "api": "responses",
            "tool_choice": args.tool_choice,
            "temperature": float(st.get("temperature", 0.3)),
            "max_tokens": int(st.get("max_tokens", 1200)),
            "search_context_size": modell.get("search_context_size", "low"),
            "parallel": parallel,
        },
        "brand": brand.name,
        "arms": {ARM_A: "tools=[web_search], tool_choice=required",
                 ARM_B: "kein tools-Feld"},
        "limit": args.limit or None,
        "seed": args.seed,
        "repeats": repeats,
        "n_prompts": len(prompts),
        "n_prompts_available": len(alle_prompts),
        "n_calls": len(records),
        "prompts_per_product": verteilung,
        "duration_seconds": round(dauer, 1),
        "cost_estimate": est,
        "cost_actual": actual_cost(records, args.web_search_price),
        "bootstrap": boot,
        "by_product": prod,
        "counter_checks": counter_checks(records),
        "errors": {
            "n_failed": len(failed),
            "arm_a": sum(1 for r in failed if r["arm"] == ARM_A),
            "arm_b": sum(1 for r in failed if r["arm"] == ARM_B),
            "top_messages": dict(sorted(msgs.items(), key=lambda kv: -kv[1])),
        },
        "dropped_pairs": dropped,
        "pairs": pairs,
        "responses": records,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_dryrun" if args.dry_run else ""
    json_path = out_dir / f"search_ab_{ts}{suffix}.json"
    md_path = out_dir / f"search_ab_{ts}{suffix}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    write_markdown(result, md_path)

    m = (boot.get("metrics") or {}).get("sov_pooled") or {}
    print("-" * 72)
    print(f"[ERGEBNIS] Paare: {boot.get('n_pairs', 0)}, "
          f"Fehler: {len(failed)}/{len(records)}, Dauer: {dauer:.0f}s")
    if m.get("diff") is not None:
        print(f"[ERGEBNIS] SoV gepoolt  A {m['arm_a'] * 100:.2f} %  "
              f"B {m['arm_b'] * 100:.2f} %  "
              f"Differenz {m['diff'] * 100:+.2f} pp "
              f"[{m['ci_low'] * 100:+.2f}; {m['ci_high'] * 100:+.2f}]")
    print(f"[SCHREIBE] {json_path}")
    print(f"[SCHREIBE] {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
